# PMS Backend

The API for the Pharmacy Management System (PMS). It is built with [FastAPI](https://fastapi.tiangolo.com/) and uses [Supabase](https://supabase.com/) as its database.

The frontend lives in a separate repository: [PMS-frontend](https://github.com/Jeevanantham-Poongodi/PMS-frontend).

> **Status:** early setup. The API currently has health and connection-test endpoints only. The pharmacy features (master data, stock ledger, access control, audit trail) are planned.

## Tech stack

- Python 3.12
- FastAPI, served by Uvicorn
- Supabase Python client
- Docker and Docker Compose
- GitHub Actions to build the image and deploy it to AWS EC2

## Project structure

```
backend/
├── main.py                  # FastAPI app, CORS setup and endpoints
├── requirements.txt         # Python dependencies
├── Dockerfile               # Container image (python:3.12-slim, non-root user)
├── docker-compose.yml       # Local development: builds the image from this folder
├── docker-compose.prod.yml  # Production: pulls the prebuilt image from GHCR
├── .dockerignore
└── .github/workflows/
    └── deploy.yml           # Build, push and deploy pipeline
```

## Environment variables

Create a `.env` file in the `backend/` folder:

```env
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_KEY=<your-supabase-key>
```

| Variable       | Description                                              |
| -------------- | -------------------------------------------------------- |
| `SUPABASE_URL` | Your Supabase project URL                                |
| `SUPABASE_KEY` | Your Supabase API key (from Project Settings → API)      |

`.env` is listed in `.gitignore` and `.dockerignore`. Never commit it.

## Running locally

### Option 1: Python virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
uvicorn main:app --reload
```

The API runs at http://localhost:8000.

### Option 2: Docker Compose

```bash
docker compose up --build
```

This builds the image, loads `.env`, exposes port `8000` and runs a health check against `/` every 30 seconds.

To stop it:

```bash
docker compose down
```

## API endpoints

| Method | Path       | Description                                                        |
| ------ | ---------- | ------------------------------------------------------------------ |
| GET    | `/`        | Health check. Returns a message and whether Supabase is initialised |
| GET    | `/hello`   | Same response as `/`                                               |
| GET    | `/test-db` | Reads one row from the Supabase `items` table to test the connection |

FastAPI also serves interactive API docs:

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## CORS

The API currently accepts requests only from the Vite dev server at `http://localhost:5173`. To allow the deployed frontend, add its URL to `allow_origins` in [main.py](main.py).

## Deployment

Every push to `main` runs [.github/workflows/deploy.yml](.github/workflows/deploy.yml). You can also run it by hand from the Actions tab.

1. **Build:** builds the Docker image and pushes it to `ghcr.io/jeevanantham-poongodi/pms-backend`, tagged with `latest` and the commit SHA.
2. **Deploy:** copies `docker-compose.prod.yml` to `~/PMS-backend` on the EC2 server, then pulls the new image and restarts the container.

### Required GitHub secrets

| Secret        | Description                           |
| ------------- | ------------------------------------- |
| `EC2_HOST`    | Public IP or hostname of the server   |
| `EC2_USER`    | SSH user (for example `ubuntu`)       |
| `EC2_SSH_KEY` | Private SSH key for that user         |

`GITHUB_TOKEN` is provided by GitHub automatically.

### Server setup (one time)

On the EC2 server:

1. Install Docker and the Docker Compose plugin.
2. Create the folder `~/PMS-backend`.
3. Put a `.env` file with the production Supabase values in that folder.
4. Open port `8000` in the instance's security group.
