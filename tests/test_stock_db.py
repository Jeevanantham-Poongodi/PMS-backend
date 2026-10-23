"""Append-only ledger and non-negative balances (constitution II, XIII; FR-012; SC-003)."""

from decimal import Decimal

import psycopg
import pytest

from app.core.errors import AppError
from app.domain import stock


def test_posting_updates_balances_and_matches_ledger(flow_env):
    batch = flow_env.stock_in("central_store", "para", "L1", 200, 50)
    stock.post_document(
        flow_env.cur,
        flow_env.user,
        "transfer",
        [
            {
                "item_id": flow_env.items["para"],
                "batch_id": batch,
                "location_id": flow_env.loc["central_store"],
                "qty": Decimal(-20),
            },
            {
                "item_id": flow_env.items["para"],
                "batch_id": batch,
                "location_id": flow_env.loc["pharmacy"],
                "qty": Decimal(20),
            },
        ],
        request_id=None,
    )
    assert flow_env.balance(batch, "central_store") == 30
    assert flow_env.balance(batch, "pharmacy") == 20
    mismatches = flow_env.cur.execute(
        "select s.batch_id from public.stock_balances s"
        " where s.hospital_id = %s and s.qty <> (select sum(qty) from public.ledger_lines l"
        "   where l.batch_id = s.batch_id and l.location_id = s.location_id)",
        (flow_env.hospital_id,),
    ).fetchall()
    assert mismatches == []


def test_negative_balance_is_refused_and_nothing_posts(flow_env):
    batch = flow_env.stock_in("pharmacy", "para", "L2", 200, 5)
    before = flow_env.cur.execute("select count(*) as n from public.ledger_lines").fetchone()["n"]
    with pytest.raises(AppError) as error:
        stock.post_document(
            flow_env.cur,
            flow_env.user,
            "issue",
            [
                {
                    "item_id": flow_env.items["para"],
                    "batch_id": batch,
                    "location_id": flow_env.loc["pharmacy"],
                    "qty": Decimal(-6),
                },
                {
                    "item_id": flow_env.items["para"],
                    "batch_id": batch,
                    "location_id": flow_env.loc["ward"],
                    "qty": Decimal(6),
                },
            ],
            request_id=None,
        )
    assert error.value.code == "INSUFFICIENT_STOCK"
    assert flow_env.balance(batch, "pharmacy") == 5
    after = flow_env.cur.execute("select count(*) as n from public.ledger_lines").fetchone()["n"]
    assert after == before


@pytest.mark.parametrize(
    "statement", ["update public.ledger_lines set qty = 1", "delete from public.ledger_lines"]
)
def test_ledger_cannot_be_changed(flow_env, statement):
    flow_env.stock_in("pharmacy", "para", "L3", 200, 5)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with flow_env.conn.transaction():
            flow_env.cur.execute(statement + " where hospital_id = %s", (flow_env.hospital_id,))


def test_balances_cannot_be_written_directly(flow_env):
    batch = flow_env.stock_in("pharmacy", "para", "L4", 200, 5)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with flow_env.conn.transaction():
            flow_env.cur.execute(
                "update public.stock_balances set qty = 999 where batch_id = %s", (batch,)
            )


def test_quarantine_location_cannot_be_allocatable(flow_env):
    with pytest.raises(psycopg.errors.CheckViolation):
        with flow_env.conn.transaction():
            flow_env.cur.execute(
                "insert into public.locations (hospital_id, code, name, kind, allocatable)"
                " values (%s, 'BAD', 'Bad', 'quarantine_qc', true)",
                (flow_env.hospital_id,),
            )
