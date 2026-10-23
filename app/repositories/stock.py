"""SQL for stock documents, the append-only ledger and balances (research R3, R4).

Balances are written only by the database trigger app.apply_ledger_line(); this module never
updates them. Runs inside a user transaction, so RLS also filters every query.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import Cursor


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
    cur.executemany(
        "insert into public.ledger_lines"
        " (hospital_id, document_id, item_id, batch_id, location_id, qty)"
        " values (%s, %s, %s, %s, %s, %s)",
        [
            (hospital_id, document_id, ln["item_id"], ln["batch_id"], ln["location_id"], ln["qty"])
            for ln in lines
        ],
    )


def set_first_receipt(cur: Cursor, hospital_id: int, batch_ids: list[int]) -> None:
    cur.execute(
        "update public.batches set first_receipt_at = now()"
        " where hospital_id = %s and id = any(%s) and first_receipt_at is null",
        (hospital_id, batch_ids),
    )


def fefo_candidates(
    cur: Cursor,
    hospital_id: int,
    item_id: int,
    location_id: int,
    today: date,
    block_on_expiry_date: bool,
) -> list[dict]:
    """Allocatable stock of an item at a location in D-005 order: expiry, first receipt, id.

    Excludes non-allocatable (quarantine) locations, blocked batches, and batches on or after
    their expiry date (D-012 temporary rule; with the setting off, the expiry date itself is
    still allowed).
    """
    cur.execute(
        "select b.id as batch_id, sb.qty, b.expiry_date, b.first_receipt_at"
        " from public.stock_balances sb"
        " join public.batches b on b.id = sb.batch_id"
        " join public.locations l on l.id = sb.location_id"
        " where sb.hospital_id = %s and sb.item_id = %s and sb.location_id = %s and sb.qty > 0"
        "   and l.allocatable and not b.blocked"
        "   and (b.expiry_date > %s or (not %s and b.expiry_date = %s))"
        " order by b.expiry_date, b.first_receipt_at nulls last, b.id",
        (hospital_id, item_id, location_id, today, block_on_expiry_date, today),
    )
    return cur.fetchall()


def balance(cur: Cursor, hospital_id: int, batch_id: int, location_id: int) -> Decimal:
    cur.execute(
        "select qty from public.stock_balances"
        " where hospital_id = %s and batch_id = %s and location_id = %s",
        (hospital_id, batch_id, location_id),
    )
    row = cur.fetchone()
    return row["qty"] if row else Decimal(0)


BALANCE_COLUMNS = (
    "sb.location_id, l.name as location_name, l.kind as location_kind, sb.item_id,"
    " i.name as item_name, sb.batch_id, b.batch_no, b.expiry_date, b.blocked, sb.qty"
)
BALANCE_FROM = (
    " from public.stock_balances sb"
    " join public.locations l on l.id = sb.location_id"
    " join public.items i on i.id = sb.item_id"
    " join public.batches b on b.id = sb.batch_id"
)


def balances(
    cur: Cursor, hospital_id: int, location_id: int | None, item_id: int | None
) -> list[dict]:
    sql = f"select {BALANCE_COLUMNS}{BALANCE_FROM} where sb.hospital_id = %s and sb.qty <> 0"
    params: list[Any] = [hospital_id]
    if location_id is not None:
        sql += " and sb.location_id = %s"
        params.append(location_id)
    if item_id is not None:
        sql += " and sb.item_id = %s"
        params.append(item_id)
    cur.execute(sql + " order by l.id, i.name, b.expiry_date, b.id", params)
    return cur.fetchall()


def batch_holdings(cur: Cursor, hospital_id: int, batch_id: int) -> list[dict]:
    cur.execute(
        f"select {BALANCE_COLUMNS}{BALANCE_FROM}"
        " where sb.hospital_id = %s and sb.batch_id = %s and sb.qty > 0 order by l.id",
        (hospital_id, batch_id),
    )
    return cur.fetchall()


def batch_dispensed(cur: Cursor, hospital_id: int, batch_id: int) -> list[dict]:
    """Dispense movements of a batch with the patient reference and prescriber (FR-025)."""
    cur.execute(
        "select d.patient_ref, d.prescriber, -ll.qty as qty, d.posted_at"
        " from public.ledger_lines ll join public.stock_documents d on d.id = ll.document_id"
        " where ll.hospital_id = %s and ll.batch_id = %s and d.doc_type = 'dispense'"
        " order by d.posted_at, d.id",
        (hospital_id, batch_id),
    )
    return cur.fetchall()


def ledger_sum(cur: Cursor, hospital_id: int, batch_id: int, location_id: int) -> Decimal:
    cur.execute(
        "select coalesce(sum(qty), 0) as total from public.ledger_lines"
        " where hospital_id = %s and batch_id = %s and location_id = %s",
        (hospital_id, batch_id, location_id),
    )
    return cur.fetchone()["total"]


def near_expiry(cur: Cursor, hospital_id: int, today: date, window_days: int) -> dict:
    """Batch-locations with allocatable stock expiring within the window (FR-031)."""
    cur.execute(
        "select count(*) as batches, coalesce(sum(sb.qty), 0) as qty"
        " from public.stock_balances sb"
        " join public.batches b on b.id = sb.batch_id"
        " join public.locations l on l.id = sb.location_id"
        " where sb.hospital_id = %s and sb.qty > 0 and l.allocatable and not b.blocked"
        "   and b.expiry_date <= %s::date + %s",
        (hospital_id, today, window_days),
    )
    return cur.fetchone()


def quarantine_totals(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select l.name as location_name, coalesce(sum(sb.qty), 0) as qty"
        " from public.locations l"
        " left join public.stock_balances sb on sb.location_id = l.id and sb.qty > 0"
        " where l.hospital_id = %s and not l.allocatable"
        " group by l.id, l.name order by l.id",
        (hospital_id,),
    )
    return cur.fetchall()


def expiry_candidates(cur: Cursor, hospital_id: int, last_date: date) -> list[dict]:
    """Batch-locations with allocatable stock expiring on or before last_date (FR-024)."""
    cur.execute(
        "select sb.batch_id, sb.location_id, sb.item_id, sb.qty, b.batch_no, b.expiry_date,"
        " i.name as item_name"
        " from public.stock_balances sb"
        " join public.batches b on b.id = sb.batch_id"
        " join public.locations l on l.id = sb.location_id"
        " join public.items i on i.id = sb.item_id"
        " where sb.hospital_id = %s and sb.qty > 0 and l.allocatable and not b.blocked"
        "   and b.expiry_date <= %s"
        " order by b.expiry_date, b.id, l.id",
        (hospital_id, last_date),
    )
    return cur.fetchall()
