"""Demo reset (feature 004, FR-001, FR-002): the account moves to a new hospital and nothing
append-only changes. Runs in a transaction that is rolled back."""

import uuid

import psycopg
import pytest

from app.core.config import get_settings
from scripts.bootstrap import create_hospital, demo_member, switch_to_new_demo_hospital

APPEND_ONLY = ["ledger_lines", "stock_documents", "audit_entries", "flow_events", "ndps_register"]


@pytest.fixture
def conn():
    url = get_settings().database_url
    if not url:
        pytest.skip("DATABASE_URL not set")
    with psycopg.connect(url) as connection:
        if connection.execute("select to_regclass('public.flow_cases')").fetchone()[0] is None:
            pytest.skip("migration 0002 not applied")
        connection.execute("begin")
        try:
            yield connection
        finally:
            connection.rollback()


def counts(conn):
    return {
        t: conn.execute(f"select count(*), coalesce(max(id), 0) from public.{t}").fetchone()
        for t in APPEND_ONLY
    }


def test_reset_switches_membership_and_keeps_history(conn):
    user_id = uuid.uuid4()
    email = f"reset-{user_id}@test.invalid"
    conn.execute(
        "insert into auth.users (id, aud, role, email) values (%s, 'authenticated',"
        " 'authenticated', %s)",
        (user_id, email),
    )
    old = create_hospital(conn, "Old demo")
    conn.execute(
        "insert into public.hospital_members (hospital_id, user_id, display_name)"
        " values (%s, %s, 'Demo Admin')",
        (old, user_id),
    )
    before = counts(conn)

    new = switch_to_new_demo_hospital(conn, str(user_id), email, "Demo Admin", "New demo")

    memberships = conn.execute(
        "select hospital_id, active from public.hospital_members where user_id = %s"
        " order by hospital_id",
        (user_id,),
    ).fetchall()
    assert memberships == [(old, False), (new, True)]
    assert demo_member(conn, email) == (str(user_id), new, "Demo Admin")
    roles = conn.execute(
        "select role_code from public.user_roles where user_id = %s and hospital_id = %s",
        (user_id, new),
    ).fetchall()
    assert roles == [("admin",)]
    flow_permissions = conn.execute(
        "select count(*) from public.role_permissions where hospital_id = %s and role_code ="
        " 'admin' and permission_code like 'flows.%%'",
        (new,),
    ).fetchone()[0]
    assert flow_permissions == 3
    assert counts(conn) == before
