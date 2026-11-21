# PokéFolio backend

A FastAPI service with synchronous SQLAlchemy 2.x and psycopg 3 connections.
Requires Python 3.10 or newer.

## Local setup

Ensure the local PostgreSQL service is running. In the repository root `.env`,
add `DATABASE_URL` using the password already configured for PostgreSQL:

```dotenv
DATABASE_URL=postgresql+psycopg://pokefolio:<URL-encoded-password>@127.0.0.1:5432/pokefolio
```

Replace `<URL-encoded-password>` with your actual password, percent-encoding
reserved URL characters such as `@`, `:`, `/`, `#`, and `%`. Do not commit this
file; the root `.gitignore` excludes `.env*`. `POSTGRES_PASSWORD` alone configures
the Docker service; the backend requires `DATABASE_URL` as well.

The backend loads the root `.env` regardless of the working directory, without
overriding existing environment variables. You can also export `DATABASE_URL`
in your shell. Restart the backend after changing connection settings.

From the repository root:

```sh
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1`
in PowerShell instead of the `source` command.

## Check the service

With the server running, open another terminal and run:

```sh
curl -i http://127.0.0.1:8000/health
```

Expect HTTP `200 OK` and the JSON body:

```json
{"status": "ok"}
```

To verify the PostgreSQL connection:

```sh
curl -i http://127.0.0.1:8000/health/db
```

This executes `SELECT 1` through SQLAlchemy and returns HTTP `200 OK` with
`{"status":"ok"}` on success. Missing or invalid configuration and database
connection failures return HTTP `503 Service Unavailable` with
`{"detail":"Database unavailable"}`. Internal errors and credentials are not
included in the response. `/health` remains available independently of the database.

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Portfolio holdings (local development only)

`GET /users/{user_id}/holdings` accepts a UUID and returns:

```json
{"holdings": [{"card_id": "brs-154", "quantity_owned": 2}]}
```

Existing users with no holdings receive `200` with `{"holdings": []}`.
Malformed UUIDs receive `422` for authenticated requests.

All three portfolio endpoints require the session cookie issued by login.
Missing, unknown, expired, or revoked sessions receive the same generic `401`.
Authenticated requests for a different path user receive `403`, including
nonexistent user IDs, before any portfolio service runs. Services receive the
authenticated owner's ID. Tests exercise the real session-cookie dependency.
Keep the service local-only until production-grade rate limiting is implemented.

Run isolated tests from `backend/` with the virtual environment activated:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

API tests override the database dependency with in-memory SQLite sessions;
they do not connect to development PostgreSQL or run Alembic migrations.

## Record transactions (local development only)

`POST /users/{user_id}/transactions` accepts a user UUID and JSON:

```json
{"card_id": "brs-154", "type": "BUY", "quantity": 2, "price_per_unit": "12.34"}
```

Use `BUY` or `SELL`, a positive integer quantity, and a nonnegative price up to
`9999999999.99` (PostgreSQL `NUMERIC(12,2)`). Send prices as decimal strings to
preserve precision. More than two decimal places, including extra trailing zeros
in decimal strings, are rejected with `422`, never silently rounded.

Success returns `201` with `id`, `user_id`, `card_id`, `type`, `quantity`,
`price_per_unit` (a decimal string), `transacted_at`, and `created_at`.
Cross-user paths return `403`; missing cards return `404`, invalid input
returns `422`, and selling more
than the user's available quantity for that card returns `409`.

The transaction service owns a single database transaction on a fresh session.
Both BUY and SELL acquire `SELECT ... FOR UPDATE` on the user row before checking
the card and holdings or inserting. Under PostgreSQL's default `READ COMMITTED`
isolation, a writer waiting for the same user sees the preceding writer's commit
in its subsequent holdings query. The lock remains held until commit or rollback;
all transaction writers must follow this protocol. Validation and insert errors
roll back the transaction. Do not change the isolation level without reviewing
these concurrency assumptions.

SQLite ignores `FOR UPDATE`. The isolated tests verify validation, persistence,
rollback, holdings changes, and user/card isolation, but cannot verify PostgreSQL
row locking. The opt-in PostgreSQL test described below verifies actual lock
contention when a dedicated test database is configured. Development PostgreSQL
is never used by these tests.

The write route uses a separate fresh database session after authentication and
ownership authorization. This preserves the transaction service's existing
transaction ownership and row locks. Keep the endpoint local-only pending the
remaining deployment protections.

## Opt-in PostgreSQL concurrency test

First have a PostgreSQL administrator provision a **new**, dedicated
`pokefolio_test` database with a dedicated non-superuser owner, for example
`pokefolio_test_runner`. Prefer a separate PostgreSQL instance or container on
loopback port 55432, with its own storage and credentials. Do not reuse the
development Compose service's volume or development credentials. The existing
Compose service provisions only the development `pokefolio` database.

If those names already exist, inspect their ownership and purpose before using
them; do not drop or recreate an existing database. On the dedicated instance,
an administrator can provision unused names with:

```sql
CREATE ROLE pokefolio_test_runner LOGIN;
-- Set a unique password interactively with psql: \password pokefolio_test_runner
CREATE DATABASE pokefolio_test OWNER pokefolio_test_runner;
```

From `backend/`, set the explicit test URL using that new role's URL-encoded
password (never commit credentials), then run:

```sh
export POKEFOLIO_TEST_DATABASE_URL='postgresql+psycopg://pokefolio_test_runner:<URL-encoded-test-password>@127.0.0.1:55432/pokefolio_test'
.venv/bin/python -m unittest tests.test_transactions_postgres -v
```

The test refuses database names other than `pokefolio_test`, checks the actual
database name and `READ COMMITTED` isolation, and creates tables only in a new
randomly named schema. It removes only that run's schema afterward, never a
database or preexisting schema. An interrupted process may leave its test schema
behind; inspect it before manual cleanup. No Alembic migrations are run.

The fixture creates a user, card, and BUY of exactly two units. Two workers use
different PostgreSQL connections and sessions to SELL two units each through
the application service. A test-only SQLAlchemy hook pauses the first worker
after `SELECT ... FOR UPDATE`; `pg_blocking_pids` must report the second worker
blocked by the first before it is released. Timeouts bound synchronization and
SQL waits. Assertions require one successful SELL, one `InsufficientHoldingsError`,
zero final holdings, and exactly one persisted SELL. This tests the service used
by HTTP requests, rather than the HTTP transport itself.

Without `POKEFOLIO_TEST_DATABASE_URL`, standard discovery skips this test and
runs SQLite tests without PostgreSQL. Different server isolation settings cause
a clear test failure; they are not silently overridden. Passing this test covers
the two-writer lock scenario, not load, deadlocks, retries, or alternate writers
that bypass the service's user-lock protocol.

## Transaction history (local development only)

`GET /users/{user_id}/transactions?limit=20&offset=0` returns only the requested
user's transactions, ordered by `transacted_at DESC`, then `created_at DESC`,
then `id DESC` to break ties deterministically. Each item uses the same saved
transaction response fields as POST, including decimal prices as strings.

The response contains `transactions`, `limit`, `offset`, and `total`. `total`
counts all transactions for that user before pagination. `limit` defaults to 20
and must be 1–100; `offset` defaults to 0 and must be nonnegative. Empty history
or an offset beyond the last result returns an empty list. Cross-user paths
receive `403`; malformed UUIDs or invalid pagination receive `422`.

Count and page queries use the session's default isolation; concurrent writes
can change results between queries or paginated requests. This endpoint has the
same local-only restriction pending production-grade rate limiting.

## Database migrations

Alembic uses `DATABASE_URL` from the environment or the root `.env`, with the
same precedence as the application. Use the `postgresql+psycopg` URL shown above.
Credentials belong in `.env` or the environment, never in `alembic.ini`.

With the virtual environment activated, run commands from `backend/`:

```sh
python -m alembic heads
python -m alembic history
```

Review the initial migration in `alembic/versions/0001_create_cards_table.py`.
It creates only `cards`; its downgrade drops that table. It has not been applied.
Applying migrations is a separate step after review; starting FastAPI does not
create tables or apply migrations.

After the initial migration has been reviewed and applied, future model changes
can be turned into candidate migrations with:

```sh
python -m alembic revision --autogenerate -m "Describe the schema change"
```

Always review generated migrations before applying them. Model imports in
`alembic/env.py` register the tables with the shared `Base.metadata`.

## Structure

```text
backend/
├── alembic/
│   ├── env.py              # Metadata and environment-based database configuration
│   ├── script.py.mako      # Revision template
│   └── versions/
│       └── 0001_create_cards_table.py
├── alembic.ini             # Alembic configuration; no credentials
├── app/
│   ├── __init__.py
│   ├── main.py             # Application creation and router registration
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py         # Shared DeclarativeBase
│   │   └── session.py      # Lazy engine and shared synchronous session factory
│   ├── models/
│   │   ├── __init__.py
│   │   └── card.py         # Card ORM model
│   └── api/
│       ├── __init__.py
│       └── routes/
│           ├── __init__.py
│           └── health.py   # GET /health and GET /health/db
├── .gitignore
├── requirements.txt
└── README.md
```

Add future route modules under `app/api/routes/` and register their routers
in `app/main.py`. The backend runs separately from the Next.js frontend.

## Login and session cookies (local development only)

`POST /auth/login` accepts the same normalized email and 12–128 character
password fields as registration. Successful login returns only the user's
`id`, `email`, and `created_at`. Invalid credentials receive the same generic
401 response. Login commits a seven-day database session containing only a
SHA-256 token digest before sending the raw token in the `pokefolio_session`
cookie. The cookie is host-only, HttpOnly, SameSite=Lax, Path=/, and has a
604800-second Max-Age. Responses use `Cache-Control: no-store`.

Cookies default to Secure, with `POKEFOLIO_ENV=production` and
`POKEFOLIO_SESSION_COOKIE_SECURE=true` as the default configuration. Keep these
settings in production and serve over HTTPS. For local HTTP development only,
explicitly set **both** `POKEFOLIO_ENV=development` and
`POKEFOLIO_SESSION_COOKIE_SECURE=false` in the environment or root `.env`, then
restart the backend. Insecure cookies are accepted only for loopback hostnames
(`localhost`, `127.0.0.1`, or `::1`). Other insecure configurations fail closed
before creating a session. Do not log request bodies or Set-Cookie headers.

`GET /auth/me` resolves the session cookie through the reusable current-user
dependency and returns only `id`, `email`, and `created_at`. Missing, unknown,
expired, revoked, or orphaned sessions receive the same generic 401 response.
Session lookup is read-only and does not extend expiry.

Production-grade rate limiting is still
required before public deployment. Keep the service local-only for now.


## Logout and session revocation

`POST /auth/logout` requires a valid active session cookie. It sets only that
session's `revoked_at` to an aware UTC timestamp and commits before returning
`204 No Content`. The row is retained and other device sessions stay active.
After commit, the response deletes the configured session cookie using the same
host-only scope and Path=/ as login, preserving the configured Secure, HttpOnly,
and SameSite=Lax attributes. Local HTTP uses the explicit development settings
documented above. Responses use `Cache-Control: no-store`.

Missing, unknown, expired, or already-revoked sessions receive the same generic
401 response. Replaying a logged-out token against `/auth/me` or any portfolio
endpoint receives 401. Database errors roll back revocation and do not send a
cookie-deletion header. Production-grade rate limiting is still required
before public deployment; session cleanup is not implemented.


## CSRF validation and trusted frontend origins

FastAPI centrally validates every HTTP method except GET, HEAD, and OPTIONS,
including extension methods. Registration, login, logout, and portfolio writes
all require a single valid Origin in `POKEFOLIO_TRUSTED_ORIGINS`. API clients
(including scripts and tests) must send a trusted Origin for unsafe methods.
This check supplements session authentication; it does not replace it.

Configure the comma-separated allowlist in the environment or root `.env`:

```dotenv
# Local development frontend:
POKEFOLIO_TRUSTED_ORIGINS=http://localhost:3000
```

In production, explicitly configure the HTTPS browser-facing frontend origin(s),
for example `https://pokefolio.example.com`. Do not include localhost in the
production allowlist unless deliberately needed. No origins are trusted by
default. Empty or malformed configuration rejects unsafe requests. Wildcards,
URL paths, userinfo, query strings, and fragments are not allowed. Comparison
uses parsed scheme, hostname, and effective port (HTTP 80, HTTPS 443), with
case-insensitive hostnames and canonical IPv6 addresses. Host, Forwarded,
X-Forwarded-Host, and Referer never grant trust.

Unsafe requests with absent, malformed, null, duplicate, or untrusted Origin
receive generic `403 {"detail":"Forbidden"}` before route dependencies run.
If Sec-Fetch-Site is present, it must be exactly `same-origin`. Cross-site,
same-site, none, duplicate, and invalid values are rejected: this application
supports same-origin browser API calls only. Fetch Metadata may be absent when
a valid Origin is supplied. Safe methods skip CSRF checks but retain their
normal authentication and ownership checks.

The Next.js API proxy preserves the original browser Origin and
Sec-Fetch-Site when forwarding requests; it does not manufacture trusted values
for requests with absent or untrusted headers. The allowlist describes the
browser-facing origin, not FastAPI's internal address. CSRF tokens, distributed rate limiting, and authentication UI remain outside this implementation. Keep
the service local-only pending the remaining deployment work. Do not log
request cookies, credentials, tokens, or response Set-Cookie headers.


## Development authentication rate limits

One development worker enforces rolling 60-second limits of **5 attempts for
POST /auth/login** and **3 attempts for POST /auth/register**, independently per
client IP and endpoint. These fixed defaults and the 4,096-bucket storage cap
are defined in `app/core/rate_limit.py`; there is no environment bypass switch.
The CSRF Origin configuration above still applies. CSRF rejection runs first
and consumes no allowance. All admitted requests count, including successful,
failed, malformed, and validation-error requests, before database dependencies
or expensive password hashing/verification. Other endpoints are unaffected.

Exceeded limits return generic `429 {"detail":"Too many requests"}`, with
`Retry-After` rounded up to whole seconds until the next attempt can be admitted,
and `Cache-Control: no-store`. Rejected attempts do not extend the window.
The limiter uses a monotonic clock, a lock for atomic admission across threads,
and bounded timestamp queues. Expired buckets are reclaimed on new identities;
when capacity is exhausted, new identities receive 429 until a bucket expires.
Active limits are not evicted to make room for other identities.

Identity uses only the ASGI peer address (ignoring source port). IPv6 addresses
are canonicalized, and missing peers share a conservative fallback bucket.
X-Forwarded-For, Forwarded, and X-Real-IP do not grant a new identity. Run local
Uvicorn with **--no-proxy-headers**, as shown in the setup command: otherwise
server middleware can rewrite the peer address before FastAPI sees it. Do not
configure an upstream component to rewrite client identity from arbitrary headers.

This is development-stage protection only: counters are **per process**, reset
on restart/reload, and have **no cross-worker or cross-instance consistency**.
Clients sharing a NAT share quotas. Behind the Next.js proxy, FastAPI
observes the proxy's shared IP, so all proxied clients share quotas.
This is not a production-ready per-client identity strategy. Before deployment,
implement a distributed limiter at the trusted ingress or a deliberately
authenticated proxy-to-backend identity mechanism that strips client-supplied
identity headers, restricts direct backend access, and forwards only validated
peer identities. An IP header alone is insufficient. Redis, distributed limits,
are not implemented here. Keep the service local-only.


## Same-origin Next.js proxy

Follow the repository [local development guide](../README.md#local-development)
to run both services. Configure `POKEFOLIO_TRUSTED_ORIGINS=http://localhost:3000`,
`POKEFOLIO_ENV=development`, and `POKEFOLIO_SESSION_COOKIE_SECURE=false` explicitly
for local HTTP. Run Uvicorn on 127.0.0.1:8000 with `--no-proxy-headers`. Next.js
runs on localhost:3000 and defaults its server-only `POKEFOLIO_BACKEND_ORIGIN` to
http://127.0.0.1:8000. Browser requests use `/api/*`, and backend route URLs stay
unchanged. Production retains Secure cookies and requires HTTPS and a production
frontend allowlist; do not reuse the local HTTP settings.

The App Router proxy preserves Origin, Fetch Metadata, Cookie, status codes, and
Set-Cookie, with no-store request/response handling. It drops forwarded identity
headers. The current limiter therefore shares one proxy-IP quota across clients;
it is not suitable for multi-user production or multiple workers. Run
`npm run test:proxy` from the root for isolated live auth/portfolio checks through
localhost:3000, with no development database writes. Stop an existing frontend on
port 3000 first. The runner uses temporary SQLite and does not apply migrations.
