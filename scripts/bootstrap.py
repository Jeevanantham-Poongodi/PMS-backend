"""One-off administration commands run by an operator with DATABASE_URL (not from the browser).

Self-registration is not allowed (FR-002), so the first administrator is set up here. Create the
person's sign-in account first in the Supabase dashboard (Authentication -> Users -> Add user).

    python scripts/bootstrap.py hospital --name "City Hospital"
    python scripts/bootstrap.py member --hospital-id 1 --email admin@example.org \
        --display-name "Admin" --role admin
    python scripts/bootstrap.py grant --hospital-id 1 --role pharmacist --permission items.read
    python scripts/bootstrap.py demo-user --email demo@pharmacy.local --password '...'

`demo-user` is for demo deployments: it creates the sign-in account itself (needs the service
role key in SUPABASE_KEY), a hospital if none exists, and makes the account an administrator.

Roles come from the actors in the flow diagram plus an administrator (FR-004). Only `admin` gets
permissions automatically; every other role's permissions are configuration (OI-6).
"""

import argparse
import sys
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, get_settings  # noqa: E402

ROLES = [
    ("store", "Store staff", False),
    ("drug_committee", "Drug committee", True),
    ("hod", "Head of department", True),
    ("finance", "Finance", True),
    ("ward", "Ward / OT / ICU staff", False),
    ("pharmacist", "Pharmacist", True),
    ("prescriber", "Doctor / prescriber", False),
    ("nurse", "Nurse", False),
    ("qc", "Quality control", False),
    ("audit", "Audit", True),
    ("admin", "Administrator", True),
]


def create_hospital(conn: psycopg.Connection, name: str) -> int:
    with conn.transaction():
        hospital_id = conn.execute(
            "insert into public.hospitals (name) values (%s) returning id", (name,)
        ).fetchone()[0]
        for code, role_name, privileged in ROLES:
            conn.execute(
                "insert into public.roles (hospital_id, code, name, privileged)"
                " values (%s, %s, %s, %s)",
                (hospital_id, code, role_name, privileged),
            )
        conn.execute(
            "insert into public.role_permissions (hospital_id, role_code, permission_code)"
            " select %s, 'admin', code from public.permissions",
            (hospital_id,),
        )
    print(f"hospital {hospital_id} created with {len(ROLES)} roles")
    return hospital_id


def add_member(
    conn: psycopg.Connection, hospital_id: int, email: str, display_name: str, role: str
) -> None:
    with conn.transaction():
        row = conn.execute(
            "select id from auth.users where lower(email) = lower(%s)", (email,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"no sign-in account for {email}; create it in Supabase first")
        conn.execute(
            "insert into public.hospital_members (hospital_id, user_id, display_name)"
            " values (%s, %s, %s) on conflict (hospital_id, user_id) do update"
            " set display_name = excluded.display_name, active = true",
            (hospital_id, row[0], display_name),
        )
        conn.execute(
            "insert into public.user_roles (hospital_id, user_id, role_code)"
            " values (%s, %s, %s) on conflict do nothing",
            (hospital_id, row[0], role),
        )
    print(f"{email} is a member of hospital {hospital_id} with role {role}")


def grant(conn: psycopg.Connection, hospital_id: int, role: str, permission: str) -> None:
    with conn.transaction():
        conn.execute(
            "insert into public.role_permissions (hospital_id, role_code, permission_code)"
            " values (%s, %s, %s) on conflict do nothing",
            (hospital_id, role, permission),
        )
    print(f"role {role} in hospital {hospital_id} has {permission}")


def create_auth_user(settings: Settings, email: str, password: str) -> None:
    """Creates a confirmed sign-in account through the Supabase Admin API; skips if it exists."""
    if not settings.supabase_url or not settings.supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY (service role) must be set in backend/.env")
    response = httpx.post(
        f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users",
        headers={
            "apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}",
        },
        json={"email": email, "password": password, "email_confirm": True},
        timeout=15,
    )
    if response.status_code == 422 and "already" in response.text:
        print(f"sign-in account {email} already exists")
        return
    response.raise_for_status()
    print(f"sign-in account {email} created")


def demo_user(
    conn: psycopg.Connection, settings: Settings, email: str, password: str, display_name: str
) -> None:
    create_auth_user(settings, email, password)
    row = conn.execute("select id from public.hospitals order by id limit 1").fetchone()
    hospital_id = row[0] if row else create_hospital(conn, "Demo Hospital")
    add_member(conn, hospital_id, email, display_name, "admin")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("hospital")
    p.add_argument("--name", required=True)
    p = sub.add_parser("member")
    p.add_argument("--hospital-id", type=int, required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--display-name", required=True)
    p.add_argument("--role", required=True, choices=[r[0] for r in ROLES])
    p = sub.add_parser("grant")
    p.add_argument("--hospital-id", type=int, required=True)
    p.add_argument("--role", required=True, choices=[r[0] for r in ROLES])
    p.add_argument("--permission", required=True)
    p = sub.add_parser("demo-user")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--display-name", default="Demo Admin")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set in backend/.env", file=sys.stderr)
        return 1
    with psycopg.connect(settings.database_url) as conn:
        if args.cmd == "hospital":
            create_hospital(conn, args.name)
        elif args.cmd == "member":
            add_member(conn, args.hospital_id, args.email, args.display_name, args.role)
        elif args.cmd == "demo-user":
            demo_user(conn, settings, args.email, args.password, args.display_name)
        else:
            grant(conn, args.hospital_id, args.role, args.permission)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
