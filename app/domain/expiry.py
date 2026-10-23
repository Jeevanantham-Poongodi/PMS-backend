"""F4:"Daily expiry scan" (FR-024): one case per batch-location with allocatable stock expiring
within the largest configured window, never duplicating an open case."""

from datetime import timedelta

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.domain.flows import TxFactory, open_case
from app.repositories import cases as cases_repo
from app.repositories import masters as masters_repo
from app.repositories import stock as stock_repo


def scan(user: CurrentUser, tx: TxFactory, request_id: str) -> dict:
    user.require("flows.write")
    created: list[int] = []
    with tx() as cur:
        hid = user.hospital_id
        windows = sorted(
            masters_repo.get_setting(cur, hid, "expiry.alert_windows_days", [90, 60, 30])
        )
        today = hospital_today(cur, hid)
        for row in stock_repo.expiry_candidates(cur, hid, today + timedelta(days=windows[-1])):
            if cases_repo.open_expiry_case_exists(cur, hid, row["batch_id"], row["location_id"]):
                continue
            days = (row["expiry_date"] - today).days
            window = next(w for w in windows if days <= w)
            created.append(
                open_case(
                    cur,
                    user,
                    flow=4,
                    start="f4_scan",
                    title=(
                        f"{row['item_name']} {row['batch_no']} expires"
                        f" {row['expiry_date'].isoformat()} ({window}-day window)"
                    ),
                    data={
                        "batch_id": row["batch_id"],
                        "location_id": row["location_id"],
                        "window_days": window,
                    },
                    lines=[{
                        "item_id": row["item_id"],
                        "batch_id": row["batch_id"],
                        "location_id": row["location_id"],
                        "qty": row["qty"],
                    }],
                    request_id=request_id,
                )
            )
    return {"created": len(created), "case_ids": created}
