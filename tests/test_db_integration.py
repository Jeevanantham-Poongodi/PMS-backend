"""Database tests for RLS, permissions and the append-only audit trail.

They need DATABASE_URL (a Supabase project with migrations applied) and are skipped otherwise.
Everything runs in one transaction that is rolled back, so no test data is left behind.
"""

import json
import uuid

import psycopg
import pytest

from app.core.config import get_settings

DATABASE_URL = get_settings().database_url
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


@pytest.fixture
def conn():
    with psycopg.connect(DATABASE_URL) as connection:
        exists = connection.execute("select to_regclass('public.items') is not null").fetchone()[0]
        if not exists:
            pytest.skip("migrations not applied (run scripts/migrate.py)")
        connection.execute("begin")
        try:
            yield connection
        finally:
            connection.rollback()


def make_user(conn, email):
    user_id = uuid.uuid4()
    conn.execute(
        "insert into auth.users (id, aud, role, email)"
        " values (%s, 'authenticated', 'authenticated', %s)",
        (user_id, email),
    )
    return user_id


def setup_hospital(conn, name, user_id, permissions):
    hospital_id = conn.execute(
        "insert into public.hospitals (name) values (%s) returning id", (name,)
    ).fetchone()[0]
    conn.execute(
        "insert into public.roles (hospital_id, code, name) values (%s, 'store', 'Store')",
        (hospital_id,),
    )
    for permission in permissions:
        conn.execute(
            "insert into public.role_permissions values (%s, 'store', %s)",
            (hospital_id, permission),
        )
    conn.execute(
        "insert into public.hospital_members (hospital_id, user_id, display_name)"
        " values (%s, %s, 'Test')",
        (hospital_id, user_id),
    )
    conn.execute("insert into public.user_roles values (%s, %s, 'store')", (hospital_id, user_id))
    return hospital_id


def act_as(conn, user_id):
    conn.execute("set local role authenticated")
    conn.execute(
        "select set_config('request.jwt.claims', %s, true),"
        " set_config('request.jwt.claim.sub', %s, true)",
        (json.dumps({"sub": str(user_id), "role": "authenticated"}), str(user_id)),
    )


def as_admin(conn):
    conn.execute("reset role")


def insert_item(conn, hospital_id, name):
    return conn.execute(
        "insert into public.items"
        " (hospital_id, name, base_unit, formulary_status, controlled_class)"
        " values (%s, %s, 'tablet', 'formulary', 'none') returning id",
        (hospital_id, name),
    ).fetchone()[0]


@pytest.fixture
def two_hospitals(conn):
    a = make_user(conn, f"a-{uuid.uuid4()}@test.invalid")
    b = make_user(conn, f"b-{uuid.uuid4()}@test.invalid")
    reader = make_user(conn, f"r-{uuid.uuid4()}@test.invalid")
    ha = setup_hospital(conn, "Hospital A", a, ["items.read", "items.write", "audit.read"])
    hb = setup_hospital(conn, "Hospital B", b, ["items.read", "items.write"])
    conn.execute(
        "insert into public.hospital_members (hospital_id, user_id, display_name)"
        " values (%s, %s, 'Reader')",
        (ha, reader),
    )
    return {"a": a, "b": b, "reader": reader, "ha": ha, "hb": hb}


def test_user_sees_only_own_hospital_items(conn, two_hospitals):
    t = two_hospitals
    act_as(conn, t["b"])
    insert_item(conn, t["hb"], "B-only item")
    act_as(conn, t["a"])
    insert_item(conn, t["ha"], "A item")
    names = [r[0] for r in conn.execute("select name from public.items")]
    assert names == ["A item"]


def test_user_cannot_write_into_another_hospital(conn, two_hospitals):
    act_as(conn, two_hospitals["a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        insert_item(conn, two_hospitals["hb"], "Smuggled")


def test_member_without_write_permission_cannot_insert(conn, two_hospitals):
    act_as(conn, two_hospitals["reader"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        insert_item(conn, two_hospitals["ha"], "Not allowed")


def test_items_cannot_be_deleted(conn, two_hospitals):
    act_as(conn, two_hospitals["a"])
    item_id = insert_item(conn, two_hospitals["ha"], "Keep me")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("delete from public.items where id = %s", (item_id,))


def test_audit_chain_and_append_only(conn, two_hospitals):
    act_as(conn, two_hospitals["a"])
    first = conn.execute(
        "select app.audit_append(%s, 'create', 'item', '1', null, '{}'::jsonb, null, 'r1')",
        (two_hospitals["ha"],),
    ).fetchone()[0]
    second = conn.execute(
        "select app.audit_append(%s, 'update', 'item', '1', '{}'::jsonb, '{}'::jsonb, 'x', 'r2')",
        (two_hospitals["ha"],),
    ).fetchone()[0]
    rows = conn.execute(
        "select id, actor_user_id, prev_hash, entry_hash from public.audit_entries"
        " where id in (%s, %s) order by id",
        (first, second),
    ).fetchall()
    assert rows[0][1] == two_hospitals["a"]
    assert rows[1][2] == rows[0][3]  # second entry chains to the first
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("update public.audit_entries set reason = 'x' where id = %s", (first,))


def test_audit_trigger_blocks_changes_even_for_owner(conn, two_hospitals):
    act_as(conn, two_hospitals["a"])
    entry = conn.execute(
        "select app.audit_append(%s, 'create', 'item', '1', null, null, null, null)",
        (two_hospitals["ha"],),
    ).fetchone()[0]
    as_admin(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("delete from public.audit_entries where id = %s", (entry,))


def test_cannot_audit_into_another_hospital(conn, two_hospitals):
    act_as(conn, two_hospitals["a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "select app.audit_append(%s, 'create', 'item', '1', null, null, null, null)",
            (two_hospitals["hb"],),
        )


def test_anon_role_has_no_access(conn, two_hospitals):
    conn.execute("set local role anon")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select * from public.items")
