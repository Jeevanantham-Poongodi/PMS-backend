"""Stock postings and FEFO allocation (FR-012, FR-013; constitution II–IV).

Stock changes only by posting a document whose ledger lines are append-only; the database keeps
balances and refuses any that would go negative. Flows call these functions inside the same
transaction as the case move that causes them.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg.errors
from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.core.errors import AppError
from app.repositories import audit, masters
from app.repositories import stock as repo

RECEIVING_TYPES = ("opening", "receipt")


@dataclass(frozen=True)
class Pick:
    batch_id: int
    batch_no: str
    expiry_date: object
    qty: Decimal


@dataclass(frozen=True)
class Allocation:
    picks: list[Pick]
    shortfall: Decimal

    @property
    def covered(self) -> bool:
        return self.shortfall == 0


def split_fefo(candidates: list[dict], qty: Decimal) -> Allocation:
    """Takes candidates in the order given (already FEFO, D-005) until qty is covered; the rest
    is the shortfall (D-013)."""
    remaining = Decimal(qty)
    picks: list[Pick] = []
    for row in candidates:
        if remaining <= 0:
            break
        take = min(remaining, Decimal(row["qty"]))
        if take > 0:
            picks.append(Pick(row["batch_id"], row["batch_no"], row["expiry_date"], take))
            remaining -= take
    return Allocation(picks, max(remaining, Decimal(0)))


def allocate_fefo(
    cur: Cursor,
    hospital_id: int,
    item_id: int,
    location_id: int,
    qty: Decimal,
    *,
    today: date | None = None,
    block_on_expiry: bool | None = None,
) -> Allocation:
    """`today` and `block_on_expiry` may be passed in when the caller already has them, to save
    two queries per allocation."""
    if block_on_expiry is None:
        block_on_expiry = bool(
            masters.get_setting(cur, hospital_id, "allocation.block_on_expiry_date", True)
        )
    if today is None:
        today = hospital_today(cur, hospital_id)
    candidates = repo.fefo_candidates(
        cur, hospital_id, item_id, location_id, today, block_on_expiry
    )
    return split_fefo(candidates, qty)


def post_document(
    cur: Cursor,
    user: CurrentUser,
    doc_type: str,
    lines: list[dict],
    *,
    request_id: str | None,
    case_id: int | None = None,
    note: str | None = None,
    patient_ref: str | None = None,
    prescriber: str | None = None,
) -> int:
    """Posts one document. Each line: item_id, batch_id, location_id, qty (signed)."""
    lines = [line for line in lines if Decimal(line["qty"]) != 0]
    if not lines:
        raise AppError(422, "VALIDATION_FAILED", "Nothing to post.")
    try:
        with cur.connection.transaction():
            document_id = repo.insert_document(
                cur, user.hospital_id, doc_type, case_id, note, patient_ref, prescriber
            )
            repo.insert_lines(cur, user.hospital_id, document_id, lines)
    except psycopg.errors.CheckViolation as exc:
        raise AppError(
            409, "INSUFFICIENT_STOCK", "Not enough stock for this movement; nothing was posted."
        ) from exc
    if doc_type in RECEIVING_TYPES:
        masters.mark_first_receipt(cur, user.hospital_id, [line["batch_id"] for line in lines])
    audit.append(
        cur,
        hospital_id=user.hospital_id,
        action="post",
        entity_type="stock_document",
        entity_id=str(document_id),
        after={
            "doc_type": doc_type,
            "case_id": case_id,
            "lines": [{k: str(v) for k, v in line.items()} for line in lines],
        },
        request_id=request_id,
    )
    return document_id


def move_lines(item_id: int, picks: list[Pick], from_id: int | None, to_id: int | None) -> list:
    """Ledger lines for moving picked batches: out of from_id, into to_id (either may be None
    when stock enters or leaves the hospital)."""
    lines = []
    for pick in picks:
        if from_id is not None:
            lines.append(
                {
                    "item_id": item_id,
                    "batch_id": pick.batch_id,
                    "location_id": from_id,
                    "qty": -pick.qty,
                }
            )
        if to_id is not None:
            lines.append(
                {
                    "item_id": item_id,
                    "batch_id": pick.batch_id,
                    "location_id": to_id,
                    "qty": pick.qty,
                }
            )
    return lines


def whole_batch_pick(cur: Cursor, hospital_id: int, batch_id: int, location_id: int) -> list[Pick]:
    """Everything of one batch at one location, regardless of allocatability (for quarantine,
    recall and returns)."""
    qty = repo.balance(cur, hospital_id, batch_id, location_id)
    batch = masters.get_batch(cur, hospital_id, batch_id)
    if qty <= 0 or batch is None:
        return []
    return [Pick(batch_id, batch["batch_no"], batch["expiry_date"], qty)]
