"""Apply versioned SQL migrations from supabase/migrations/ in filename order.

Each file runs in its own transaction and is recorded in app.schema_migrations, so running the
script again only applies new files. Usage (from backend/):

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # list applied and pending migrations
"""

import argparse
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set in backend/.env", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("create schema if not exists app")
        conn.execute(
            "create table if not exists app.schema_migrations ("
            " filename text primary key, applied_at timestamptz not null default now())"
        )
        conn.commit()
        applied = {r[0] for r in conn.execute("select filename from app.schema_migrations")}

        for path in files:
            state = "applied" if path.name in applied else "pending"
            if args.status or state == "applied":
                print(f"{state:8} {path.name}")
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "insert into app.schema_migrations (filename) values (%s)", (path.name,)
                )
            print(f"applied  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
