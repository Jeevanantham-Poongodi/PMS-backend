"""One-off administration commands run by an operator with DATABASE_URL (not from the browser).

Self-registration is not allowed (FR-002), so the first administrator is set up here. Create the
person's sign-in account first in the Supabase dashboard (Authentication -> Users -> Add user).

    python scripts/bootstrap.py hospital --name "City Hospital"
    python scripts/bootstrap.py member --hospital-id 1 --email admin@example.org \
        --display-name "Admin" --role admin
    python scripts/bootstrap.py grant --hospital-id 1 --role pharmacist --permission items.read
    python scripts/bootstrap.py demo-user --email demo@pharmacy.local --password '...'
    python scripts/bootstrap.py demo-data --email demo@pharmacy.local

`demo-user` is for demo deployments: it creates the sign-in account itself (needs the service
role key in SUPABASE_KEY), a hospital if none exists, and makes the account an administrator.
`demo-data` loads fictional data for every flow into that account's hospital, acting as that
account (app/demo_data.py); stages already loaded are skipped.

Roles come from the actors in the flow diagram plus an administrator (FR-004). Only `admin` gets
permissions automatically; every other role's permissions are configuration (OI-6).
"""

import argparse
import sys
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.auth import CurrentUser  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.demo_data import Seeder, act_as  # noqa: E402

# Settings every hospital starts with (constitution VIII): F4 alert windows and the D-012
# temporary rule. Migration 0002 seeds them for hospitals that existed before it.
DEFAULT_SETTINGS = {
    "expiry.alert_windows_days": "[90, 60, 30]",
    "allocation.block_on_expiry_date": "true",
}

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
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "insert into public.hospital_settings (hospital_id, key, value)"
                " values (%s, %s, %s::jsonb)",
                (hospital_id, key, value),
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


def demo_data(conn: psycopg.Connection, email: str) -> None:
    """Loads demo data into the account's hospital, acting as that account under RLS."""
    row = conn.execute(
        "select m.hospital_id, m.user_id::text, m.display_name from public.hospital_members m"
        " join auth.users u on u.id = m.user_id"
        " where lower(u.email) = lower(%s) and m.active",
        (email,),
    ).fetchall()
    if len(row) != 1:
        raise SystemExit(f"{email} must be an active member of exactly one hospital")
    hospital_id, user_id, display_name = row[0]
    permissions = frozenset(
        r[0]
        for r in conn.execute(
            "select distinct rp.permission_code from public.user_roles ur"
            " join public.role_permissions rp"
            "   on rp.hospital_id = ur.hospital_id and rp.role_code = ur.role_code"
            " where ur.hospital_id = %s and ur.user_id = %s::uuid",
            (hospital_id, user_id),
        )
    )
    user = CurrentUser(
        user_id=user_id,
        hospital_id=hospital_id,
        display_name=display_name,
        roles=frozenset({"admin"}),
        permissions=permissions,
        privileged=True,
        mfa_satisfied=True,
        claims={"sub": user_id, "role": "authenticated"},
    )
    with conn.transaction():
        act_as(conn, user)
        Seeder(conn, user).run()
    print(f"demo data loaded into hospital {hospital_id}")


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
    p = sub.add_parser("demo-data")
    p.add_argument("--email", default=None, help="defaults to DEMO_EMAIL")
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
        elif args.cmd == "demo-data":
            demo_data(conn, args.email or settings.demo_email)
        else:
            grant(conn, args.hospital_id, args.role, args.permission)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
