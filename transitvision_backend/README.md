# TransitVision — Flask + PostgreSQL

TransitVision preserves the supplied station-guidance interface and station dataset while adding passenger accounts and a persistent backend. This is a local project package; it has **not** been deployed.

## Included

- Existing mobile-first station and platform guidance interface, OCR from ticket images, voice guidance, English/Roman Hinglish mode, themes and text scaling.
- Nine station records migrated from the supplied HTML into `stations_seed.json`; the database is seeded on first start and retains the seed order.
- Email/password registration and sign-in, password hashing, sign-out, and server-side sessions that expire after 12 hours by default.
- Guest browsing without server-side guest history.
- Per-account saved routes, the ten most recent searches, and frequently used stations derived from search history.
- PostgreSQL support for deployment and SQLite fallback for local development.
- CSRF tokens for mutating requests, HTTP-only session cookies, SameSite=Lax cookies, basic request rate limits, and server-side ownership checks for saved data.
- Non-GPS flags/alerts are excluded. Browser-side GPS location tracking and the destination proximity alarm (within 2 km) are retained; tracking starts only after the passenger activates it and grants permission, can be stopped at any time, and is never sent to the backend. No QR scanner is included.

## Run locally (Windows)

Python 3.10+ is recommended.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY = [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
python app.py
```

Open `http://127.0.0.1:5000`. SQLite is used locally unless `DATABASE_URL` is set. The database file is created under `instance/`.

## Environment variables

| Variable | Required / default | Purpose |
|---|---|---|
| `DATABASE_URL` | Local SQLite by default | PostgreSQL connection string in production |
| `SECRET_KEY` | Must be set for a real deployment | Signs Flask's CSRF/session cookie; keep secret |
| `COOKIE_SECURE` | `false` locally | Set `true` when serving HTTPS |
| `LOGIN_SESSION_HOURS` | `12` | Expiration for a passenger login session |
| `PORT` | `5000` | Port used by the production server |

For PostgreSQL, use the Render database's **internal connection string** for a service in the same Render region. The app converts Render's `postgres://` / `postgresql://` style URL to the `psycopg` SQLAlchemy driver automatically.

## Render + GitHub deployment preparation

1. Push this project folder to a GitHub repository.
2. Create a PostgreSQL database in Render.
3. Create a Render Web Service connected to the repository, using:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python run.py`
4. Add environment variables `DATABASE_URL` (Render internal database connection string), `SECRET_KEY` (a long random secret), `COOKIE_SECURE=true`, and optionally `LOGIN_SESSION_HOURS=12`.
5. Use HTTPS and never commit secrets or database credentials.

`render.yaml` is included as a service template; the database must be created and `DATABASE_URL` configured in Render. **Do not deploy until local tests pass and the project owner explicitly approves deployment.**

## API overview

- `GET /api/health`
- `GET /api/stations`
- `GET /api/stations/<station_key>`
- `GET /api/auth/me`
- `POST /api/auth/register` — name, email, password (minimum 8 characters)
- `POST /api/auth/login` — email, password
- `POST /api/auth/logout`
- `GET /api/profile` — profile and frequently used stations (authenticated)
- `GET /api/saved`, `POST /api/saved`, `DELETE /api/saved`, `DELETE /api/saved/<id>` (authenticated)
- `GET /api/history`, `POST /api/history`, `DELETE /api/history` (authenticated)

The browser obtains a CSRF token from `/api/auth/me` and sends it as `X-CSRF-Token` for state-changing requests. Passenger session tokens are random, HTTP-only cookies; only their SHA-256 hashes are stored in the database. Saved routes and history are scoped by the authenticated user ID, not by a browser-supplied identifier.

## Testing

Run `pytest -q` after installing dependencies. Tests cover guest access restrictions, registration/login/logout, private data isolation, route/history persistence and station data. A syntax check of the Python files and inline JavaScript is also part of the implementation checks.

## GPS privacy and browser requirements

The GPS proximity feature uses the browser Geolocation API and the destination station coordinates in the supplied dataset. Browser location access generally requires HTTPS in deployment (localhost is permitted for development). Location is processed in the browser only; it is not posted to Flask or stored in the database. The alarm threshold is 2 km and the alarm is throttled so it does not repeat continuously while the passenger remains nearby. Station coordinates are inherited from the supplied dataset and should be verified before real-world reliance.

## Station-data accuracy

The station records are preserved from the supplied HTML and have not been independently verified by railway authorities. The database migration does not make the content official. Confirm platform directions, emergency information, facility locations and coordinates before relying on them in real-world navigation.
