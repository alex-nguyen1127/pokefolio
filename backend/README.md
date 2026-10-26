# PokéFolio backend

A minimal FastAPI service. Requires Python 3.10 or newer.

## Local setup

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

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Structure

```text
backend/
├── app/
│   ├── __init__.py
│   ├── main.py             # Application creation and router registration
│   └── api/
│       ├── __init__.py
│       └── routes/
│           ├── __init__.py
│           └── health.py   # GET /health
├── .gitignore
├── requirements.txt
└── README.md
```

Add future route modules under `app/api/routes/` and register their routers
in `app/main.py`. The backend runs separately from the Next.js frontend.
