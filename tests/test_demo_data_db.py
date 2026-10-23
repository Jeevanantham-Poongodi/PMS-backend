"""The demo-data loader on an empty hospital (FR-033, SC-005), in a transaction that is rolled
back. Needs DATABASE_URL with migrations applied; skipped otherwise."""

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.demo_data import STAGES, Seeder, act_as
from app.flows.definitions import is_end

DATABASE_URL = get_settings().database_url
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")

PERMISSIONS = ("flows.read", "flows.write", "flows.approve", "stock.read", "masters.read",
               "items.read", "items.write")


@pytest.fixture(scope="module")
def seeded():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not conn.execute("select to_regclass('public.flow_cases') is not null as ok").fetchone()[
            "ok"
        ]:
            pytest.skip("migration 0002 not applied")
        try:
            hid = conn.execute(
                "insert into public.hospitals (name) values ('Demo data test') returning id"
            ).fetchone()["id"]
            conn.execute(
                "insert into public.roles (hospital_id, code, name) values (%s, 'admin', 'Admin')",
                (hid,),
            )
            for permission in PERMISSIONS:
                conn.execute(
                    "insert into public.role_permissions values (%s, 'admin', %s)",
                    (hid, permission),
                )
            conn.execute(
                "insert into public.hospital_settings values (%s, 'expiry.alert_windows_days',"
                " '[90, 60, 30]'), (%s, 'allocation.block_on_expiry_date', 'true')",
                (hid, hid),
            )
            user_id = uuid.uuid4()
            conn.execute(
                "insert into auth.users (id, aud, role, email)"
                " values (%s, 'authenticated', 'authenticated', %s)",
                (user_id, f"{user_id}@example.test"),
            )
            conn.execute(
                "insert into public.hospital_members (hospital_id, user_id, display_name)"
                " values (%s, %s, 'Demo')",
                (hid, user_id),
            )
            conn.execute("insert into public.user_roles values (%s, %s, 'admin')", (hid, user_id))
            user = CurrentUser(
                str(user_id), hid, "Demo", frozenset({"admin"}), frozenset(PERMISSIONS), True,
                True, {"sub": str(user_id), "role": "authenticated"},
            )
            act_as(conn, user)
            first = Seeder(conn, user).run(log=lambda _: None)
            second = Seeder(conn, user).run(log=lambda _: None)
            yield conn, hid, first, second
        finally:
            conn.rollback()


def test_loads_every_stage_once(seeded):
    _, _, first, second = seeded
    assert first == list(STAGES)
    assert second == []


def test_every_flow_has_cases_at_three_nodes_including_an_end(seeded):
    conn, hid, *_ = seeded
    rows = conn.execute(
        "select flow, array_agg(distinct node) as nodes from public.flow_cases"
        " where hospital_id = %s group by flow order by flow",
        (hid,),
    ).fetchall()
    assert [r["flow"] for r in rows] == [1, 2, 3, 4, 5]
    for row in rows:
        assert len(row["nodes"]) >= 3, row
        assert any(is_end(row["flow"], node) for node in row["nodes"]), row


def test_demo_tour_steps_have_a_case_waiting(seeded):
    conn, hid, *_ = seeded
    for flow, node in [(1, "f1_qc"), (2, "f2_review"), (3, "f3_order"), (4, "f4_writeoff"),
                       (5, "f5_dispensed")]:
        found = conn.execute(
            "select count(*) as n from public.flow_cases"
            " where hospital_id = %s and flow = %s and node = %s and not closed",
            (hid, flow, node),
        ).fetchone()["n"]
        assert found >= 1, (flow, node)


def test_balances_equal_ledger_sums_and_none_negative(seeded):
    conn, hid, *_ = seeded
    row = conn.execute(
        "select count(*) filter (where sb.qty <> (select coalesce(sum(l.qty), 0)"
        "   from public.ledger_lines l where l.hospital_id = sb.hospital_id"
        "   and l.batch_id = sb.batch_id and l.location_id = sb.location_id)) as mismatched,"
        " count(*) filter (where sb.qty < 0) as negative"
        " from public.stock_balances sb where sb.hospital_id = %s",
        (hid,),
    ).fetchone()
    assert row == {"mismatched": 0, "negative": 0}
