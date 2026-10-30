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
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
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
Unknown users receive `404`; malformed UUIDs receive `422`.

This endpoint has no authentication or authorization. Keep the server bound to
`127.0.0.1` as shown above. Do not expose it publicly through hosting, proxies,
or tunnels until authentication and per-user authorization are implemented.

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
Missing users/cards return `404`, invalid input returns `422`, and selling more
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

This write endpoint is also unauthenticated and local-only. Keep the server bound
to `127.0.0.1`; do not publish or tunnel it before authentication and per-user
authorization are implemented.

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
or an offset beyond the last result returns an empty list. Unknown users receive
`404`; malformed UUIDs or invalid pagination receive `422`.

Count and page queries use the session's default isolation; concurrent writes
can change results between queries or paginated requests. This endpoint has the
same local-only restriction: do not expose it publicly before authentication and
per-user authorization are implemented.

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
