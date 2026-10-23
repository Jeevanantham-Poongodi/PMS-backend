"""Read-only views of masters and stock: locations, vendors, batches, balances, batch trace and
the dashboard (FR-031). Balances are the sum of posted ledger lines (SC-003)."""

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.core.errors import AppError
from app.domain.flows import TxFactory, node_label
from app.repositories import cases as cases_repo
from app.repositories import masters as repo
from app.repositories import stock as stock_repo


def locations(user: CurrentUser, tx: TxFactory) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return repo.list_locations(cur, user.hospital_id)


def vendors(user: CurrentUser, tx: TxFactory) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return repo.list_vendors(cur, user.hospital_id)


def batches(user: CurrentUser, tx: TxFactory, item_id: int | None) -> list[dict]:
    user.require("masters.read")
    with tx() as cur:
        return repo.list_batches(cur, user.hospital_id, item_id)


def balances(
    user: CurrentUser, tx: TxFactory, location_id: int | None, item_id: int | None
) -> list[dict]:
    user.require("stock.read")
    with tx() as cur:
        return stock_repo.balances(cur, user.hospital_id, location_id, item_id)


def batch_trace(user: CurrentUser, tx: TxFactory, batch_id: int) -> dict:
    """Where a batch is and which patients received it (F5)."""
    user.require("stock.read")
    with tx() as cur:
        batch = repo.get_batch(cur, user.hospital_id, batch_id)
        if batch is None:
            raise AppError(404, "NOT_FOUND", "Batch not found.")
        return {
            "batch": batch,
            "holdings": stock_repo.batch_holdings(cur, user.hospital_id, batch_id),
            "dispensed": stock_repo.batch_dispensed(cur, user.hospital_id, batch_id),
        }


def dashboard(user: CurrentUser, tx: TxFactory) -> dict:
    user.require("flows.read")
    with tx() as cur:
        hid = user.hospital_id
        today = hospital_today(cur, hid)
        windows = sorted(repo.get_setting(cur, hid, "expiry.alert_windows_days", [90, 60, 30]))
        return {
            "open_cases": [
                {**row, "node_label": node_label(row["flow"], row["node"])}
                for row in cases_repo.open_cases_by_node(cur, hid)
            ],
            "near_expiry": [
                {"window_days": w, **stock_repo.near_expiry(cur, hid, today, w)} for w in windows
            ],
            "quarantine": stock_repo.quarantine_totals(cur, hid),
        }
