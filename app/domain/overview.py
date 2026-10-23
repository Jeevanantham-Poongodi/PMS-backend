"""Read-only views: masters, stock balances, batch trace (F5) and the dashboard (FR-031)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.core.errors import AppError
from app.domain.expiry import windows
from app.flows.definitions import FLOWS
from app.repositories import cases, masters
from app.repositories import stock as repo

TxFactory = Callable[[], AbstractContextManager[Cursor]]


def locations(user: CurrentUser, tx: TxFactory) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return masters.list_locations(cur, user.hospital_id)


def vendors(user: CurrentUser, tx: TxFactory) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return masters.list_vendors(cur, user.hospital_id)


def batches(user: CurrentUser, tx: TxFactory, item_id: int | None) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return masters.list_batches(cur, user.hospital_id, item_id)


def balances(
    user: CurrentUser, tx: TxFactory, location_id: int | None, item_id: int | None
) -> list[dict]:
    user.require("stock.read")
    with tx() as cur:
        return repo.list_balances(cur, user.hospital_id, location_id, item_id)


def trace(user: CurrentUser, tx: TxFactory, batch_id: int) -> dict:
    user.require("stock.read")
    with tx() as cur:
        batch = masters.get_batch(cur, user.hospital_id, batch_id)
        if batch is None:
            raise AppError(404, "NOT_FOUND", "Batch not found.")
        return {
            "batch": batch,
            "holdings": repo.batch_holdings(cur, user.hospital_id, batch_id),
            "dispensed": repo.batch_dispensings(cur, user.hospital_id, batch_id),
        }


def dashboard(user: CurrentUser, tx: TxFactory) -> dict:
    user.require("flows.read")
    with tx() as cur:
        alert = windows(cur, user.hospital_id)
        rows = repo.near_expiry(
            cur, user.hospital_id, hospital_today(cur, user.hospital_id), alert[-1]
        )
        buckets = {w: {"window_days": w, "batches": 0, "qty": Decimal(0)} for w in alert}
        for row in rows:
            window = next(w for w in alert if row["days_left"] <= w)
            buckets[window]["batches"] += 1
            buckets[window]["qty"] += row["qty"]
        return {
            "open_cases": [
                {**r, "node_label": FLOWS[r["flow"]].node(r["node"]).label}
                for r in cases.counts_by_node(cur, user.hospital_id)
            ],
            "near_expiry": list(buckets.values()),
            "quarantine": repo.quarantine_totals(cur, user.hospital_id),
        }
