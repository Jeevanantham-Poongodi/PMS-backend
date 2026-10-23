"""SQL for stock documents, the ledger and balances. Runs inside a user transaction (RLS)."""

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import Cursor

BALANCE_COLUMNS = (
    "s.location_id, l.name as location_name, l.kind as location_kind, s.item_id,"
    " i.name as item_name, s.batch_id, b.batch_no, b.expiry_date, b.blocked, s.qty"
)
BALANCE_FROM = (
    " from public.stock_balances s"
    " join public.locations l on l.id = s.location_id"
    " join public.items i on i.id = s.item_id"
    " join public.batches b on b.id = s.batch_id"
)


def insert_document(
    cur: Cursor,
    hospital_id: int,
    doc_type: str,
    case_id: int | None,
    note: str | None,
    patient_ref: str | None,
    prescriber: str | None,
) -> int:
    cur.execute(
        "insert into public.stock_documents"
        " (hospital_id, doc_type, case_id, note, patient_ref, prescriber)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (hospital_id, doc_type, case_id, note, patient_ref, prescriber),
    )
    return cur.fetchone()["id"]


def insert_lines(cur: Cursor, hospital_id: int, document_id: int, lines: list[dict]) -> None:
    # executemany pipelines the inserts: one round trip instead of one per line.
    cur.executemany(
        "insert into public.ledger_lines"
        " (hospital_id, document_id, item_id, batch_id, location_id, qty)"
        " values (%s, %s, %s, %s, %s, %s)",
        [
            (
                hospital_id,
                document_id,
                line["item_id"],
                line["batch_id"],
                line["location_id"],
                line["qty"],
            )
            for line in lines
        ],
    )


def fefo_candidates(
    cur: Cursor,
    hospital_id: int,
    item_id: int,
    location_id: int,
    today: date,
    block_on_expiry_date: bool,
) -> list[dict]:
    """Allocatable stock of an item at a location in FEFO order (D-005): expiry date, then first
    receipt, then batch ID. Excludes blocked batches, non-allocatable locations (constitution III)
    and batches on or after expiry (D-012 temporary rule, from configuration)."""
    expiry_test = "b.expiry_date > %s" if block_on_expiry_date else "b.expiry_date >= %s"
    cur.execute(
        "select s.batch_id, b.batch_no, b.expiry_date, s.qty"
        " from public.stock_balances s"
        " join public.batches b on b.id = s.batch_id"
        " join public.locations l on l.id = s.location_id"
        " where s.hospital_id = %s and s.item_id = %s and s.location_id = %s and s.qty > 0"
        f" and l.allocatable and not b.blocked and {expiry_test}"
        " order by b.expiry_date, b.first_receipt_at nulls last, b.id",
        (hospital_id, item_id, location_id, today),
    )
    return cur.fetchall()


def balance(cur: Cursor, hospital_id: int, batch_id: int, location_id: int) -> Decimal:
    cur.execute(
        "select qty from public.stock_balances"
        " where hospital_id = %s and batch_id = %s and location_id = %s",
        (hospital_id, batch_id, location_id),
    )
    row = cur.fetchone()
    return Decimal(0) if row is None else row["qty"]


def list_balances(
    cur: Cursor, hospital_id: int, location_id: int | None, item_id: int | None
) -> list[dict]:
    sql = f"select {BALANCE_COLUMNS}{BALANCE_FROM} where s.hospital_id = %s and s.qty > 0"
    params: list[Any] = [hospital_id]
    if location_id is not None:
        sql += " and s.location_id = %s"
        params.append(location_id)
    if item_id is not None:
        sql += " and s.item_id = %s"
        params.append(item_id)
    cur.execute(sql + " order by l.id, i.name, b.expiry_date", params)
    return cur.fetchall()


def batch_holdings(cur: Cursor, hospital_id: int, batch_id: int) -> list[dict]:
    cur.execute(
        f"select {BALANCE_COLUMNS}{BALANCE_FROM}"
        " where s.hospital_id = %s and s.batch_id = %s and s.qty > 0 order by l.id",
        (hospital_id, batch_id),
    )
    return cur.fetchall()


def batch_dispensings(cur: Cursor, hospital_id: int, batch_id: int) -> list[dict]:
    """Dispense movements of a batch with the patient they went to (F5 trace)."""
    cur.execute(
        "select d.patient_ref, d.prescriber, -sum(ll.qty) as qty, d.posted_at"
        " from public.ledger_lines ll join public.stock_documents d on d.id = ll.document_id"
        " where ll.hospital_id = %s and ll.batch_id = %s and d.doc_type = 'dispense'"
        " group by d.id order by d.posted_at",
        (hospital_id, batch_id),
    )
    return cur.fetchall()


def near_expiry(cur: Cursor, hospital_id: int, today: date, max_days: int) -> list[dict]:
    """Allocatable balances whose batch expires within max_days (F4 scan and dashboard)."""
    cur.execute(
        "select s.batch_id, s.location_id, s.item_id, s.qty, b.expiry_date, b.batch_no,"
        " i.name as item_name, (b.expiry_date - %s) as days_left"
        " from public.stock_balances s"
        " join public.batches b on b.id = s.batch_id"
        " join public.locations l on l.id = s.location_id"
        " join public.items i on i.id = s.item_id"
        " where s.hospital_id = %s and s.qty > 0 and l.allocatable and not b.blocked"
        " and b.expiry_date <= %s + %s order by b.expiry_date",
        (today, hospital_id, today, max_days),
    )
    return cur.fetchall()


def quarantine_totals(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select l.name as location_name, coalesce(sum(s.qty), 0) as qty"
        " from public.locations l left join public.stock_balances s on s.location_id = l.id"
        " where l.hospital_id = %s and not l.allocatable group by l.id order by l.id",
        (hospital_id,),
    )
    return cur.fetchall()
