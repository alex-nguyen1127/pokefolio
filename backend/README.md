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
