# PMS Backend

The API for the Pharmacy Management System: FastAPI on Python 3.12, talking to Supabase
PostgreSQL with psycopg 3. Schema changes are Alembic migrations.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Create `backend/.env` (git-ignored; [.env.example](.env.example) lists the keys):

| Variable | What it is |
| --- | --- |
| `SUPABASE_URL` | Supabase project URL |
| `DATABASE_URL` | Postgres connection string (Supabase → Connect → Session pooler) |
| `SUPABASE_JWT_MODE` | `jwks` for projects with signing keys (the default), else `secret` |
| `SUPABASE_JWT_SECRET` | Only for `secret` mode |
| `CORS_ORIGINS` | Browser origins allowed to call the API, e.g. `http://localhost:5173` |
| `SUPABASE_KEY` | Service-role key. Used only by `scripts/bootstrap.py demo-user`; never by the API |
| `DEMO_MODE` | `true` for the demo: one account may approve its own cases and give both NDPS sign-offs (D-018) |

## Database migrations

```bash
.venv/bin/alembic upgrade head      # apply
.venv/bin/alembic current           # where the database is
.venv/bin/alembic check             # models and database match
```

To add a table: add a model to [app/models.py](app/models.py), run
`alembic revision --autogenerate -m "..."`, then add row-level security, grants and policies by
hand in the new revision (the template reminds you). Never change the schema in the Supabase
dashboard.

| Revision | Contents |
| --- | --- |
| 0001 | Hospitals, members, roles, permissions, audit trail, items |
| 0002 | Locations, vendors, batches, stock ledger and balances, flow cases, NDPS register, settings |
| 0003 | Fix: balance trigger for outgoing stock |

## Demo account and data

```bash
.venv/bin/python scripts/bootstrap.py demo-user --email demo@pharmacy.local --password '<password>'
.venv/bin/python scripts/bootstrap.py demo-data     # once, into the account's hospital
.venv/bin/python scripts/bootstrap.py demo-reset    # a fresh copy before each presentation
```

- `demo-user` creates the sign-in account (needs `SUPABASE_KEY`), a hospital if none exists, and
  makes the account its administrator.
- `demo-data` loads fictional locations, vendors, 30 drugs, batches with near-expiry and expired
  stock, opening stock, and about 23 cases spread over every flow. It drives the cases through the
  same services as the API, so their history and stock are real. It runs in stages and resumes if
  the connection drops.
- `demo-reset` creates a new demo hospital, moves the demo account to it and loads fresh demo
  data. Nothing is deleted: ledger, audit and history are append-only, so the previous demo
  hospital stays in the database, inactive for the account. Takes about a minute.

Other operator commands: `hospital`, `member`, `grant` (see the top of
[scripts/bootstrap.py](scripts/bootstrap.py)).

## Running

```bash
.venv/bin/uvicorn main:app --reload        # http://localhost:8000
```

Interactive API docs: http://localhost:8000/docs. The contract for the flow endpoints is
[specs/002-flow-pages/contracts/openapi.yaml](../specs/002-flow-pages/contracts/openapi.yaml).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/health` | API and database status |
| `GET /api/v1/me` | The signed-in user's hospital, roles and permissions |
| `GET/POST /api/v1/items`, `PATCH /api/v1/items/{id}` | Item master |
| `GET /api/v1/flows` | The five flows as data (boxes and arrows) |
| `GET/POST /api/v1/cases`, `GET /api/v1/cases/{id}` | Cases of a flow |
| `POST /api/v1/cases/{id}/actions` | Take one arrow from the case's current box |
| `POST /api/v1/expiry/scan` | Daily expiry scan |
| `GET /api/v1/stock/balances`, `GET /api/v1/batches/{id}/trace` | Stock and recall trace |
| `GET /api/v1/dashboard`, `/locations`, `/vendors`, `/batches` | Dashboard and masters |

Errors always look like `{"error": {"code", "message", "details", "request_id"}}`.

## Code layout

```
app/
├── api/v1/          routes and request/response schemas
├── domain/          business rules: flows.py (case engine), effects.py (stock effects and
│                    data-driven decisions), stock.py (posting, FEFO), expiry.py, overview.py
├── flows/           definitions.py: the flow diagram as data
├── repositories/    SQL
├── core/            settings, sign-in, database, errors
└── models.py        table definitions for Alembic
scripts/             bootstrap.py (operator commands), demo_data.py
alembic/             migrations
tests/               pytest
```

## Tests

```bash
.venv/bin/pytest            # ~115 tests; database tests use DATABASE_URL and roll back
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Database tests create a throwaway hospital inside a transaction that is rolled back, so they
leave no data behind. They take several minutes against the hosted database.

## Docker and deployment

`docker compose up --build` runs the API with `.env`. The GitHub workflow in
[.github/workflows/deploy.yml](.github/workflows/deploy.yml) builds the image and deploys it to
EC2; it predates the current configuration and must be given the variables above before use.
Hosting is not decided yet (decisions D-002, D-003).
