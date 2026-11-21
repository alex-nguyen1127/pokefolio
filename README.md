# PokéFolio

Next.js App Router frontend with a same-origin `/api/*` proxy to the FastAPI
backend. Authentication UI is not implemented yet.

## Local development

Install frontend dependencies with `npm ci`. Set up the backend virtual
environment and its database as described in [backend/README.md](backend/README.md).
Keep database credentials in the existing ignored root `.env`, never in source.

Run FastAPI in one terminal, from `backend/`:

```sh
export POKEFOLIO_TRUSTED_ORIGINS=http://localhost:3000
export POKEFOLIO_ENV=development
export POKEFOLIO_SESSION_COOKIE_SECURE=false
.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Run Next.js in another terminal, from the repository root:

```sh
# Optional: this server-only destination defaults to http://127.0.0.1:8000.
export POKEFOLIO_BACKEND_ORIGIN=http://127.0.0.1:8000
npm run dev -- --port 3000
```

Open **http://localhost:3000**. Use that hostname consistently: `127.0.0.1:3000`
is a different browser origin and is not in the allowlist above. These settings
can also live in the ignored root `.env`; restart both servers after changes.
The backend reads root `.env`, while Next.js also supports `.env.local`.

## API transport

Browser requests use relative `/api/*` URLs. The server maps `/api/auth/me` to
FastAPI `/auth/me`, preserving method, query, JSON body, Cookie, Origin, and
Fetch Metadata. The destination is server-only `POKEFOLIO_BACKEND_ORIGIN`, never
a `NEXT_PUBLIC_` variable or a request-supplied URL. It must be an HTTP(S) origin
without credentials, path, query, or fragment.

An external rewrite was tested first with this installed Next.js version.
Its configured cache header was absent on a proxied validation-error response.
The implementation therefore uses `src/app/api/[[...path]]/route.ts` to enforce
`cache: no-store` upstream and `Cache-Control: private, no-store` on **all** API
responses. FastAPI status codes and individual Set-Cookie headers pass through.
Backend slash redirects stay in the browser-facing `/api` namespace. Upstream
failures return generic 502 responses without internal details.

Origin and Sec-Fetch-Site are never synthesized or made trusted by the proxy.
Missing or untrusted Origin and inconsistent Fetch Metadata still receive 403
from FastAPI on unsafe requests. Scripts must supply a configured trusted Origin.
The proxy neither forwards nor generates forwarded client-identity headers.
FastAPI continues to validate sessions, enforce ownership, and commit writes.

Session cookies are host-only, HttpOnly, SameSite=Lax, Path=/, with a seven-day
Max-Age. Set-Cookie is forwarded without exposing tokens in JSON or browser
JavaScript. Logout clears the cookie and persists revocation. **Secure defaults
remain enabled in production**; only the explicit loopback development settings
above allow HTTP cookies. Production requires HTTPS and an explicit production
frontend-origin allowlist. Do not log cookies, credentials, or Set-Cookie headers.

## Checks

```sh
npm run lint
npx tsc --noEmit
npm run build
npm run test:proxy
```

`test:proxy` requires the backend virtual environment with
`backend/requirements-dev.txt` installed. Stop any existing frontend on port
3000 first. The runner starts real Next.js and FastAPI processes and sends
requests through **localhost:3000**. It creates a temporary SQLite database,
forbids the development database factory, uses isolated `.next` output, restores
Next-generated TypeScript configuration edits, and removes its servers and test
data afterward. It never prints bodies, credentials, cookies, or tokens.

The script checks registration, login, `/auth/me`, logout cookie deletion and
revocation, replay rejection, portfolio ownership and writes, query forwarding,
CSRF rejection, no-store responses, and shared rate limits. It also starts a
production-cookie backend profile to verify that Secure is preserved and that
an HTTP client does not send that cookie over HTTP. This is not an HTTPS or
production deployment test. `POKEFOLIO_NEXT_DIST_DIR` is an optional server-side
build-directory override used by the integration runner.

Run the complete backend suite from `backend/` with:

```sh
.venv/bin/python -m unittest discover -s tests -q
```

## Production limitations

The development in-memory rate limiter sees Next.js as a **shared client IP**.
All proxied users therefore share registration/login quotas. Counters are
per-process, reset on restart, and have no cross-worker consistency. Before
multi-user production use, add distributed limits and explicitly trusted client
identity at ingress or across an authenticated proxy boundary; do not enable
arbitrary forwarded-IP trust. Keep FastAPI private and use `--no-proxy-headers`
for the current development topology. Production deployment and distributed
limiting are outside this step.
