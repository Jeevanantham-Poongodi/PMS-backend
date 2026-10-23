"""F4:"Daily expiry scan" (FR-024): one case per batch-location with allocatable stock inside the
largest alert window, never duplicating an open case. Windows are configuration (VIII)."""

from collections.abc import Callable
from contextlib import AbstractContextManager

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.domain.flows import open_case
from app.repositories import cases, masters
from app.repositories import stock as stock_repo

TxFactory = Callable[[], AbstractContextManager[Cursor]]


def windows(cur: Cursor, hospital_id: int) -> list[int]:
    value = masters.get_setting(cur, hospital_id, "expiry.alert_windows_days", [90, 60, 30])
    return sorted(int(w) for w in value)


def scan_cursor(cur: Cursor, user: CurrentUser, request_id: str | None) -> list[int]:
    alert = windows(cur, user.hospital_id)
    today = hospital_today(cur, user.hospital_id)
    created = []
    for row in stock_repo.near_expiry(cur, user.hospital_id, today, alert[-1]):
        if cases.open_expiry_case_exists(
            cur, user.hospital_id, row["batch_id"], row["location_id"]
        ):
            continue
        window = next(w for w in alert if row["days_left"] <= w)
        case = open_case(
            cur,
            user,
            4,
            "f4_scan",
            f"{row['item_name']} {row['batch_no']} expires {row['expiry_date']}"
            f" ({window}-day window)",
            {"batch_id": row["batch_id"], "location_id": row["location_id"], "window_days": window},
            [
                {
                    "item_id": row["item_id"],
                    "qty": row["qty"],
                    "batch_id": row["batch_id"],
                    "location_id": row["location_id"],
                }
            ],
            request_id,
        )
        created.append(case["id"])
    return created


def scan(user: CurrentUser, tx: TxFactory, request_id: str | None) -> list[int]:
    user.require("flows.write")
    with tx() as cur:
        return scan_cursor(cur, user, request_id)
