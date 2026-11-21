"""Live proxy checks through localhost:3000, using a disposable SQLite backend.

Run with backend/.venv/bin/python scripts/test_api_proxy.py (or npm run test:proxy).
No existing servers or development databases are used. Output never includes
request bodies, cookies, credentials, tokens, or subprocess logs.
"""

from contextlib import contextmanager
from hashlib import sha256
from http.cookies import SimpleCookie
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://localhost:3000"
COOKIE_NAME = "pokefolio_session"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def server(command, cwd, environment):
    # Logs can contain request information; keep them private and never dump them.
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=log,
                                   stderr=log, start_new_session=True)
        try:
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            log.seek(0)
            process.upgrade_advisory = b"known security vulnerability" in log.read()
            if process.upgrade_advisory:
                print("NOTICE: installed Next.js reports a security advisory; upgrade before production")


@contextmanager
def preserve_next_config():
    # next dev adds temporary distDir type globs to these files. Restore exactly
    # their original bytes after the test, including when a subprocess fails.
    paths = [ROOT / "tsconfig.json", ROOT / "next-env.d.ts"]
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        yield
    finally:
        for path, original in snapshots.items():
            if original is not None:
                path.write_bytes(original)
            elif path.exists():
                path.unlink()


def wait_ready(url, process):
    deadline = time.monotonic() + 50
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            check(process.poll() is None, f"Test server exited before readiness on port {httpx.URL(url).port} (exit {process.poll()})")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise AssertionError("Test server readiness timed out")


def request(client, method, path, expected, **kwargs):
    try:
        response = client.request(method, "/api" + path, **kwargs)
    except httpx.HTTPError as error:
        raise AssertionError(f"Transport failure for {method} {path}: {type(error).__name__}") from None
    check(response.status_code == expected, f"Unexpected status for {method} {path}")
    check("no-store" in response.headers.get("cache-control", ""), f"Missing no-store for {path} (status {response.status_code}; cache-control present: {bool(response.headers.get('cache-control'))})")
    return response


def cookie_flags(response, secure):
    cookie = SimpleCookie(response.headers.get("set-cookie", "")).get(COOKIE_NAME)
    check(cookie is not None, "Session Set-Cookie did not pass through proxy")
    check(bool(cookie["httponly"]) and cookie["samesite"].lower() == "lax", "Unsafe cookie attributes")
    check(cookie["path"] == "/" and not cookie["domain"], "Cookie scope changed")
    check(bool(cookie["secure"]) == secure, "Secure cookie setting changed in transit")
    return cookie


def run_workflow(database, secure):
    password = secrets.token_urlsafe(24)
    email = f"proxy-{uuid4().hex}@example.com"
    credentials = {"email": email, "password": password}
    with httpx.Client(base_url=ORIGIN, trust_env=False, timeout=15,
                      headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}) as client:
        # Missing Origin must not be manufactured by the proxy.
        with httpx.Client(base_url=ORIGIN, trust_env=False, timeout=15) as anonymous:
            request(anonymous, "POST", "/auth/register", 403, json=credentials)
        request(client, "POST", "/auth/register", 403, json=credentials,
                headers={"Origin": "https://untrusted.example", "X-Forwarded-Host": "localhost:3000"})
        request(client, "POST", "/auth/register", 403, json=credentials,
                headers={"Sec-Fetch-Site": "cross-site"})
        request(client, "POST", "/auth/login", 422, json={})
        registered = request(client, "POST", "/auth/register", 201, json=credentials)
        user = registered.json()
        check(set(user) == {"id", "email", "created_at"}, "Unsafe registration response fields")
        check(user["email"] == email, "Request body did not reach backend")
        request(client, "POST", "/auth/register", 409, json=credentials)
        request(client, "POST", "/auth/login", 401,
                json={"email": email, "password": "synthetic incorrect password"})
        login = request(client, "POST", "/auth/login", 200, json=credentials)
        check(login.json() == user, "Unsafe or unexpected login response")
        cookie = cookie_flags(login, secure)
        check(cookie["max-age"] == "604800", "Cookie lifetime changed")
        token = cookie.value
        check(token not in login.text and password not in login.text, "Sensitive JSON response")
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT token_hash, revoked_at FROM sessions").fetchall()
        check(len(rows) == 1 and rows[0][0] == sha256(token.encode()).hexdigest(), "Session digest storage failed")
        if secure:
            # HTTP clients must not send a Secure cookie back over local HTTP.
            request(client, "GET", "/auth/me", 401)
            print("PASS: production Secure/HttpOnly/SameSite cookie attributes preserved")
            return
        check(request(client, "GET", "/auth/me", 200).json() == user, "Cookie not forwarded to /auth/me")
        user_id = user["id"]
        request(client, "GET", f"/users/{user_id}/holdings", 200)
        request(client, "GET", f"/users/{uuid4()}/holdings", 403)
        request(client, "POST", f"/users/{uuid4()}/transactions", 403, json={
            "card_id": "proxy-test-card", "type": "BUY", "quantity": 1, "price_per_unit": "2.50",
        })
        transaction = request(client, "POST", f"/users/{user_id}/transactions", 201, json={
            "card_id": "proxy-test-card", "type": "BUY", "quantity": 2, "price_per_unit": "2.50",
        }).json()
        check(transaction["user_id"] == user_id and transaction["quantity"] == 2, "Portfolio POST body changed")
        holdings = request(client, "GET", f"/users/{user_id}/holdings", 200).json()
        check(holdings == {"holdings": [{"card_id": "proxy-test-card", "quantity_owned": 2}]}, "Stale portfolio response")
        history = request(client, "GET", f"/users/{user_id}/transactions?limit=1&offset=0", 200).json()
        check(history["limit"] == 1 and history["total"] == 1, "Query string did not pass through proxy")
        # Same URL without Cookie must not reuse an authenticated response.
        with httpx.Client(base_url=ORIGIN, trust_env=False, timeout=15) as anonymous:
            request(anonymous, "GET", "/auth/me", 401)
            request(anonymous, "GET", f"/users/{user_id}/holdings", 401)
        logout = request(client, "POST", "/auth/logout", 204)
        check(logout.content == b"", "Logout must have an empty body")
        deletion = cookie_flags(logout, False)
        check(deletion["max-age"] == "0" and deletion["expires"], "Cookie deletion attributes changed")
        check(COOKIE_NAME not in client.cookies, "Logout failed to clear client cookie")
        request(client, "GET", "/auth/me", 401)
        with sqlite3.connect(database) as connection:
            row = connection.execute("SELECT revoked_at FROM sessions").fetchone()
        check(row is not None and row[0] is not None, "Logout failed to persist revocation")
        # Replay the removed cookie to prove server-side revocation, not just deletion.
        client.cookies.set(COOKIE_NAME, token)
        request(client, "GET", "/auth/me", 401)
        request(client, "GET", f"/users/{user_id}/holdings", 401)
        request(client, "POST", "/auth/logout", 401)
        # All forwarded identity headers remain untrusted: quota stays shared.
        request(client, "POST", "/auth/login", 401, json={"email": email, "password": "synthetic wrong password"})
        request(client, "POST", "/auth/login", 401, json={"email": email, "password": "synthetic wrong password"})
        throttled = request(client, "POST", "/auth/login", 429, json=credentials, headers={
            "X-Forwarded-For": "192.0.2.123", "X-Real-IP": "192.0.2.124", "Forwarded": "for=192.0.2.125",
        })
        check(int(throttled.headers.get("retry-after", "0")) > 0, "Retry-After was not forwarded")
        print("PASS: registration/login/me/logout, session revocation, portfolio ownership, CSRF, caching, shared quota")


def main():
    # Refuse to send test mutations to an existing development frontend.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 3000))
        except OSError:
            raise AssertionError("Port 3000 is occupied; stop the existing frontend before this isolated test") from None
    (ROOT / ".next").mkdir(exist_ok=True)
    backend_port = unused_port()
    with preserve_next_config(), tempfile.TemporaryDirectory(prefix="pokefolio-proxy-") as temporary, \
         tempfile.TemporaryDirectory(prefix="proxy-test-", dir=ROOT / ".next") as dist:
        environment = os.environ.copy()
        environment.update({
            "DATABASE_URL": "", "POKEFOLIO_TEST_DATABASE_URL": "",
            "POKEFOLIO_TRUSTED_ORIGINS": ORIGIN,
            "POKEFOLIO_BACKEND_ORIGIN": f"http://127.0.0.1:{backend_port}",
            "POKEFOLIO_NEXT_DIST_DIR": str(Path(dist).relative_to(ROOT)),
            "NEXT_TELEMETRY_DISABLED": "1",
        })
        frontend_command = ["node", "node_modules/next/dist/bin/next", "dev", "--hostname", "127.0.0.1", "--port", "3000"]
        for secure in (False, True):
            # Some installed Next versions stop once to report an upgrade
            # advisory. One retry with the same build directory honors their
            # documented continuation behavior, without upgrading dependencies.
            for attempt in range(2):
                database = Path(temporary) / f"profile-{secure}-{attempt}.sqlite"
                backend_env = environment | {
                    "POKEFOLIO_PROXY_TEST_DATABASE": str(database),
                    "POKEFOLIO_ENV": "production" if secure else "development",
                    "POKEFOLIO_SESSION_COOKIE_SECURE": "true" if secure else "false",
                }
                command = [sys.executable, "-m", "uvicorn", "tests.proxy_backend:app", "--host", "127.0.0.1",
                           "--port", str(backend_port), "--no-proxy-headers", "--no-access-log", "--log-level", "warning"]
                try:
                    with server(command, ROOT / "backend", backend_env) as backend, \
                         server(frontend_command, ROOT, environment) as frontend:
                        wait_ready(f"http://127.0.0.1:{backend_port}/health", backend)
                        wait_ready(ORIGIN + "/api/health", frontend)
                        run_workflow(database, secure)
                    break
                except AssertionError:
                    if attempt or not getattr(frontend, "upgrade_advisory", False):
                        raise
    print("PASS: isolated proxy integration complete; test processes and database removed")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, httpx.HTTPError, OSError) as error:
        # Fixed check messages and exception types only; never dump responses/logs.
        message = str(error) if isinstance(error, AssertionError) else type(error).__name__
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
