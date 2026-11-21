"""Deterministic auth limiter tests; no real sleeps or database access."""

from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.csrf import CSRFMiddleware
from app.core.rate_limit import AuthRateLimiter, AuthRateLimitMiddleware
from app.main import app


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.limiter = AuthRateLimiter(clock=lambda: self.now)
        self.calls = 0
        probe = FastAPI()
        probe.state.auth_rate_limiter = self.limiter
        probe.add_middleware(AuthRateLimitMiddleware)
        probe.add_middleware(CSRFMiddleware)

        @probe.api_route("/{path:path}", methods=["GET", "POST"])
        def endpoint(request: Request):
            self.calls += 1
            return JSONResponse({"ok": True}, status_code=int(request.query_params.get("status", "200")))

        self.probe = probe
        env = patch.dict("os.environ", {"POKEFOLIO_TRUSTED_ORIGINS": "http://localhost:3000"})
        env.start()
        self.addCleanup(env.stop)
        self.client = self.make_client("192.0.2.1")

    def make_client(self, host, port=1234):
        client = TestClient(self.probe, client=(host, port), headers={"Origin": "http://localhost:3000"})
        self.addCleanup(client.close)
        return client

    def test_login_and_registration_thresholds(self):
        for path, limit in (("/auth/login", 5), ("/auth/register", 3)):
            for _ in range(limit):
                self.assertEqual(self.client.post(path).status_code, 200)
            response = self.client.post(path)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json(), {"detail": "Too many requests"})
            self.assertEqual(response.headers["retry-after"], "60")
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.calls, 8)

    def test_success_and_failure_outcomes_share_quota(self):
        for status in (200, 401, 422, 500, 200):
            self.assertEqual(self.client.post(f"/auth/login?status={status}").status_code, status)
        self.assertEqual(self.client.post("/auth/login?status=200").status_code, 429)

    def test_rolling_window_boundary_and_retry_after_rounding(self):
        for _ in range(5):
            self.client.post("/auth/login")
        self.now += 0.1
        self.assertEqual(self.client.post("/auth/login").headers["retry-after"], "60")
        self.now = 159.2
        self.assertEqual(self.client.post("/auth/login").headers["retry-after"], "1")
        self.now = 160.0
        self.assertEqual(self.client.post("/auth/login").status_code, 200)

    def test_window_is_rolling_and_rejections_do_not_extend_it(self):
        self.client.post("/auth/register")
        self.now = 110.0
        self.client.post("/auth/register")
        self.now = 120.0
        self.client.post("/auth/register")
        self.now = 159.0
        self.assertEqual(self.client.post("/auth/register").headers["retry-after"], "1")
        self.now = 160.0
        self.assertEqual(self.client.post("/auth/register").status_code, 200)
        self.assertEqual(self.client.post("/auth/register").headers["retry-after"], "10")

    def test_endpoint_and_client_isolation(self):
        for _ in range(5):
            self.client.post("/auth/login")
        self.assertEqual(self.client.post("/auth/login").status_code, 429)
        self.assertEqual(self.client.post("/auth/register").status_code, 200)
        self.assertEqual(self.make_client("192.0.2.2").post("/auth/login").status_code, 200)
        self.assertEqual(self.make_client("192.0.2.1", port=9999).post("/auth/login").status_code, 429)

    def test_forwarding_headers_cannot_change_identity(self):
        for _ in range(5):
            self.client.post("/auth/login")
        for index in range(3):
            response = self.client.post("/auth/login", headers={
                "X-Forwarded-For": f"198.51.100.{index}",
                "Forwarded": f"for=198.51.100.{index}", "X-Real-IP": f"198.51.100.{index}",
            })
            self.assertEqual(response.status_code, 429)

    def test_csrf_rejections_do_not_consume_allowance(self):
        for _ in range(8):
            self.assertEqual(self.client.post("/auth/login", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.calls, 0)
        for _ in range(5):
            self.assertEqual(self.client.post("/auth/login").status_code, 200)
        self.assertEqual(self.client.post("/auth/login").status_code, 429)
        self.assertEqual(self.client.post("/auth/login", headers={"Origin": "null"}).status_code, 403)

    def test_other_endpoints_and_safe_methods_unaffected(self):
        for _ in range(8):
            self.assertEqual(self.client.get("/auth/login").status_code, 200)
            self.assertEqual(self.client.post("/auth/logout").status_code, 200)
            self.assertEqual(self.client.post("/users/example/transactions").status_code, 200)
        for _ in range(5):
            self.assertEqual(self.client.post("/auth/login").status_code, 200)

    def test_concurrent_admissions_are_atomic(self):
        for path, limit in (("/auth/login", 5), ("/auth/register", 3)):
            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(executor.map(lambda _: self.limiter.retry_after("192.0.2.1", path), range(50)))
            self.assertEqual(results.count(0), limit)
            self.assertEqual(results.count(60), 50 - limit)

    def test_capacity_is_bounded_and_active_buckets_are_not_evicted(self):
        limiter = AuthRateLimiter(clock=lambda: self.now, max_buckets=2)
        for _ in range(5):
            self.assertEqual(limiter.retry_after("192.0.2.1", "/auth/login"), 0)
        self.assertEqual(limiter.retry_after("192.0.2.2", "/auth/login"), 0)
        for index in range(3, 100):
            self.assertEqual(limiter.retry_after(f"192.0.2.{index}", "/auth/login"), 60)
        self.assertEqual(limiter.retry_after("192.0.2.1", "/auth/login"), 60)
        self.assertEqual(len(limiter._buckets), 2)
        self.now += 60
        self.assertEqual(limiter.retry_after("192.0.2.3", "/auth/register"), 0)
        self.assertEqual(len(limiter._buckets), 1)

    def test_missing_peer_and_equivalent_ipv6_share_identity(self):
        for host in ("::1", "0:0:0:0:0:0:0:1"):
            client = self.make_client(host)
            for _ in range(3):
                client.post("/auth/login")
        self.assertEqual(client.post("/auth/login").status_code, 429)
        with TestClient(self.probe, client=None, headers={"Origin": "http://localhost:3000"}) as client:
            for _ in range(3):
                self.assertEqual(client.post("/auth/register").status_code, 200)
            self.assertEqual(client.post("/auth/register").status_code, 429)

    def test_trailing_slash_uses_same_quota(self):
        for _ in range(5):
            self.client.post("/auth/login")
        self.assertEqual(self.client.post("/auth/login/", follow_redirects=False).status_code, 429)

    def test_real_app_limits_before_validation_database_and_argon2(self):
        with patch.object(app.state, "auth_rate_limiter", self.limiter):
            with TestClient(app, headers={"Origin": "http://localhost:3000"}) as client:
                with patch("app.db.session.get_session_factory", side_effect=AssertionError(
                    "Throttled requests must not access the database"
                )) as factory, patch("app.services.auth.hash_password") as hash_password, \
                     patch("app.services.auth.verify_password") as verify_password:
                    for path, limit in (("/auth/login", 5), ("/auth/register", 3)):
                        for _ in range(limit):
                            self.assertEqual(self.limiter.retry_after("testclient", path), 0)
                        response = client.post(path, json={})
                        self.assertEqual(response.status_code, 429)
                        self.assertEqual(response.headers["retry-after"], "60")
                    factory.assert_not_called()
                    hash_password.assert_not_called()
                    verify_password.assert_not_called()


    def test_capacity_retry_waits_for_a_whole_bucket_to_expire(self):
        limiter = AuthRateLimiter(clock=lambda: self.now, max_buckets=1)
        self.assertEqual(limiter.retry_after("192.0.2.1", "/auth/login"), 0)
        self.now += 10
        self.assertEqual(limiter.retry_after("192.0.2.1", "/auth/login"), 0)
        self.assertEqual(limiter.retry_after("192.0.2.2", "/auth/login"), 60)
        self.now += 50
        self.assertEqual(limiter.retry_after("192.0.2.2", "/auth/login"), 10)
        self.now += 10
        self.assertEqual(limiter.retry_after("192.0.2.2", "/auth/login"), 0)

    def test_real_app_validation_failures_count_toward_limit(self):
        from app.db.session import get_session

        def no_database_session():
            yield None

        previous = app.dependency_overrides.copy()
        self.addCleanup(setattr, app, "dependency_overrides", previous)
        app.dependency_overrides[get_session] = no_database_session
        with patch.object(app.state, "auth_rate_limiter", self.limiter):
            with TestClient(app, headers={"Origin": "http://localhost:3000"}) as client:
                for path, limit in (("/auth/login", 5), ("/auth/register", 3)):
                    for _ in range(limit):
                        self.assertEqual(client.post(path, json={}).status_code, 422)
                    self.assertEqual(client.post(path, json={}).status_code, 429)
