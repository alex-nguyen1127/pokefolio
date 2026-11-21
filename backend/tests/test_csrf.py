"""Centralized CSRF policy tests; no database connections or real credentials."""

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.csrf import CSRFMiddleware
from app.main import app


class CSRFTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS":
                                      "http://localhost:3000,https://frontend.example.com"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = 0
        probe = FastAPI()
        probe.add_middleware(CSRFMiddleware)

        @probe.api_route("/probe", methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH",
                                            "DELETE", "TRACE", "PROPFIND", "CUSTOM"])
        def endpoint():
            self.calls += 1
            return {"ok": True}

        self.client = TestClient(probe)
        self.addCleanup(self.client.close)

    def assert_forbidden(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Forbidden"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.calls, 0)

    def test_trusted_origins_and_effective_ports(self):
        for origin in ("http://localhost:3000", "https://frontend.example.com",
                       "https://FRONTEND.example.com:443", "HTTPS://frontend.example.com"):
            with self.subTest(origin=origin):
                response = self.client.post("/probe", headers={"Origin": origin})
                self.assertEqual(response.status_code, 200)

    def test_untrusted_and_malformed_origins(self):
        for origin in (None, "", "null", "http://localhost:3001", "https://localhost:3000",
                       "http://localhost", "https://frontend.example.com.evil.example",
                       "https://evilfrontend.example.com", "https://frontend.example.com:444",
                       "https://frontend.example.com/", "https://frontend.example.com/path",
                       "https://frontend.example.com?x", "https://frontend.example.com#x",
                       "https://user@frontend.example.com", "https://frontend.example.com@evil.example",
                       "https://frontend.example.com:", "https://frontend.example.com:99999",
                       "https://frontend.example.com:0", "https://frontend.example.com:abc",
                       "https://frontend.example.com https://evil.example",
                       "https://frontend.example.com,https://evil.example", "https://[invalid]",
                       " https://frontend.example.com", "https://frontend.example.com\t",
                       "https://frontend..example.com", "ftp://frontend.example.com",
                       "https://frontend.example.com\\evil.example"):
            with self.subTest():
                headers = {} if origin is None else {"Origin": origin}
                self.assert_forbidden(self.client.post("/probe", headers=headers))

    def test_duplicate_headers_rejected(self):
        self.assert_forbidden(self.client.post("/probe", headers=[
            ("Origin", "http://localhost:3000"), ("Origin", "http://localhost:3000"),
        ]))
        self.assert_forbidden(self.client.post("/probe", headers=[
            ("Origin", "http://localhost:3000"),
            ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin"),
        ]))

    def test_fetch_metadata_must_match_same_origin_architecture(self):
        for site in ("cross-site", "same-site", "none", "invalid", "", "Same-Origin",
                     "same-origin, cross-site"):
            with self.subTest(site=site):
                self.assert_forbidden(self.client.post("/probe", headers={
                    "Origin": "http://localhost:3000", "Sec-Fetch-Site": site,
                }))
        for headers in ({"Origin": "http://localhost:3000"},
                        {"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-origin"}):
            self.assertEqual(self.client.post("/probe", headers=headers).status_code, 200)

    def test_fetch_metadata_does_not_replace_origin_validation(self):
        self.assert_forbidden(self.client.post("/probe", headers={"Sec-Fetch-Site": "same-origin"}))
        self.assert_forbidden(self.client.post("/probe", headers={
            "Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin",
        }))

    def test_safe_methods_skip_csrf(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            for headers in ({}, {"Origin": "null", "Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(self.client.request(method, "/probe", headers=headers).status_code, 200)
        self.assertEqual(self.calls, 6)

    def test_all_unsafe_methods_protected_including_extensions(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "PROPFIND", "CUSTOM"):
            self.assert_forbidden(self.client.request(method, "/probe"))
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "PROPFIND", "CUSTOM"):
            self.assertEqual(self.client.request(method, "/probe", headers={
                "Origin": "http://localhost:3000",
            }).status_code, 200)

    def test_host_forwarded_headers_and_referer_do_not_grant_trust(self):
        self.assert_forbidden(self.client.post("/probe", headers={
            "Origin": "https://evil.example", "Host": "evil.example",
            "Forwarded": "host=evil.example;proto=https", "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https", "Referer": "http://localhost:3000/",
        }))

    def test_missing_or_invalid_configuration_fails_closed(self):
        for configured in ("", "*", "http://localhost:3000,", "https://frontend.example.com/path"):
            with patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": configured}):
                self.assert_forbidden(self.client.post("/probe", headers={"Origin": "http://localhost:3000"}))

    def test_ipv6_and_explicit_default_http_port(self):
        with patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://[::1]:3000,http://localhost:80"}):
            for origin in ("http://[0:0:0:0:0:0:0:1]:3000", "http://localhost"):
                self.assertEqual(self.client.post("/probe", headers={"Origin": origin}).status_code, 200)

    def test_application_mutations_blocked_before_dependencies(self):
        with TestClient(app) as client:
            with patch("app.db.session.get_session_factory", side_effect=AssertionError(
                "CSRF rejection must precede database access"
            )) as factory:
                for path in ("/auth/register", "/auth/login", "/auth/logout",
                             "/users/00000000-0000-0000-0000-000000000001/transactions"):
                    for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.example"}):
                        self.assert_forbidden(client.post(path, headers=headers, json={}))
                factory.assert_not_called()
