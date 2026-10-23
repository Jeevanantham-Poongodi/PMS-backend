"""Stock layer (FR-010 to FR-013): documents post signed ledger lines; balances follow by trigger
and can never go negative; allocation is FEFO (D-005), split across batches (D-013).

Flows change stock only through post_document (constitution XI).
"""

from dataclasses import dataclass
from decimal import Decimal

import psycopg.errors
from psycopg import Cursor

from app.core.dates import hospital_today
from app.core.errors import AppError
from app.repositories import audit
from app.repositories import masters as masters_repo
from app.repositories import stock as repo

ZERO = Decimal(0)
# Documents that bring a batch into the hospital; they set first_receipt_at (D-005 step 2).
RECEIPT_TYPES = {"opening", "receipt"}


@dataclass(frozen=True)
class Pick:
    batch_id: int
    qty: Decimal


def to_qty(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def split_fefo(candidates: list[dict], qty: Decimal) -> tuple[list[Pick], Decimal]:
    """Takes `qty` from candidates already in FEFO order (D-013); returns picks and shortfall."""
    picks: list[Pick] = []
    remaining = qty
    for row in candidates:
        if remaining <= ZERO:
            break
        take = min(Decimal(row["qty"]), remaining)
        if take > ZERO:
            picks.append(Pick(row["batch_id"], take))
            remaining -= take
    return picks, max(remaining, ZERO)


def allocate_fefo(
    cur: Cursor, hospital_id: int, item_id: int, location_id: int, qty: Decimal
) -> tuple[list[Pick], Decimal]:
    block_on_expiry = masters_repo.get_setting(
        cur, hospital_id, "allocation.block_on_expiry_date", True
    )
    candidates = repo.fefo_candidates(
        cur, hospital_id, item_id, location_id, hospital_today(cur, hospital_id), block_on_expiry
    )
    return split_fefo(candidates, qty)


def available(cur: Cursor, hospital_id: int, item_id: int, location_id: int) -> Decimal:
    block_on_expiry = masters_repo.get_setting(
        cur, hospital_id, "allocation.block_on_expiry_date", True
    )
    candidates = repo.fefo_candidates(
        cur, hospital_id, item_id, location_id, hospital_today(cur, hospital_id), block_on_expiry
    )
    return sum((Decimal(r["qty"]) for r in candidates), ZERO)


def move(item_id: int, batch_id: int, source: int, target: int, qty: Decimal) -> list[dict]:
    """The two ledger lines of a movement between locations."""
    return [
        {"item_id": item_id, "batch_id": batch_id, "location_id": source, "qty": -qty},
        {"item_id": item_id, "batch_id": batch_id, "location_id": target, "qty": qty},
    ]


def fefo_moves(
    cur: Cursor,
    hospital_id: int,
    item_id: int,
    source: int,
    target: int | None,
    qty: Decimal,
    what: str,
) -> tuple[list[dict], list[Pick]]:
    """Ledger lines taking `qty` of an item out of `source` by FEFO, into `target` if given.

    Refuses with INSUFFICIENT_STOCK when allocatable stock does not cover the quantity.
    """
    picks, shortfall = allocate_fefo(cur, hospital_id, item_id, source, qty)
    if shortfall > ZERO:
        raise AppError(
            409,
            "INSUFFICIENT_STOCK",
            f"Not enough usable stock {what}: short by {shortfall.normalize():f}.",
            {"item_id": item_id, "shortfall": str(shortfall)},
        )
    lines: list[dict] = []
    for pick in picks:
        if target is None:
            lines.append(
                {"item_id": item_id, "batch_id": pick.batch_id, "location_id": source,
                 "qty": -pick.qty}
            )
        else:
            lines.extend(move(item_id, pick.batch_id, source, target, pick.qty))
    return lines, picks


def post_document(
    cur: Cursor,
    hospital_id: int,
    doc_type: str,
    lines: list[dict],
    *,
    case_id: int | None = None,
    note: str | None = None,
    patient_ref: str | None = None,
    prescriber: str | None = None,
    request_id: str | None = None,
) -> int | None:
    """Posts one stock document and its ledger lines, with its audit entry. Lines with a zero
    quantity are dropped; nothing is posted if none remain."""
    lines = [ln for ln in lines if Decimal(ln["qty"]) != ZERO]
    if not lines:
        return None
    try:
        document_id = repo.insert_document(
            cur, hospital_id, doc_type, case_id, note, patient_ref, prescriber
        )
        repo.insert_lines(cur, hospital_id, document_id, lines)
    except psycopg.errors.CheckViolation as exc:
        if exc.diag.constraint_name == "stock_balances_qty_check":
            raise AppError(
                409, "INSUFFICIENT_STOCK", "This would make a stock balance negative."
            ) from exc
        raise
    if doc_type in RECEIPT_TYPES:
        repo.set_first_receipt(
            cur, hospital_id, sorted({ln["batch_id"] for ln in lines if ln["qty"] > 0})
        )
    audit.append(
        cur,
        hospital_id=hospital_id,
        action="post",
        entity_type="stock_document",
        entity_id=str(document_id),
        after={
            "doc_type": doc_type,
            "case_id": case_id,
            "lines": [{k: str(v) for k, v in ln.items()} for ln in lines],
        },
        request_id=request_id,
    )
    return document_id
