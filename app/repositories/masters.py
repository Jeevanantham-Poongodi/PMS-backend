"""SQL for locations, vendors, batches and hospital settings. Runs inside a user transaction, so
RLS also filters every query."""

import json
from datetime import date
from typing import Any

from psycopg import Cursor

BATCH_COLUMNS = (
    "b.id, b.item_id, i.name as item_name, b.batch_no, b.expiry_date, b.vendor_id, b.blocked"
)


def list_locations(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select id, code, name, kind, allocatable from public.locations"
        " where hospital_id = %s order by id",
        (hospital_id,),
    )
    return cur.fetchall()


def get_location(cur: Cursor, hospital_id: int, location_id: int) -> dict | None:
    cur.execute(
        "select id, code, name, kind, allocatable from public.locations"
        " where hospital_id = %s and id = %s",
        (hospital_id, location_id),
    )
    return cur.fetchone()


def location_by_kind(cur: Cursor, hospital_id: int, kind: str) -> dict | None:
    """The first location of a kind (one pharmacy, one central store, one of each quarantine)."""
    cur.execute(
        "select id, code, name, kind, allocatable from public.locations"
        " where hospital_id = %s and kind = %s order by id limit 1",
        (hospital_id, kind),
    )
    return cur.fetchone()


def list_vendors(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select id, name from public.vendors where hospital_id = %s and active order by name",
        (hospital_id,),
    )
    return cur.fetchall()


def list_batches(cur: Cursor, hospital_id: int, item_id: int | None) -> list[dict]:
    sql = (
        f"select {BATCH_COLUMNS} from public.batches b join public.items i on i.id = b.item_id"
        " where b.hospital_id = %s"
    )
    params: list[Any] = [hospital_id]
    if item_id is not None:
        sql += " and b.item_id = %s"
        params.append(item_id)
    cur.execute(sql + " order by i.name, b.expiry_date, b.id limit 1000", params)
    return cur.fetchall()


def get_batch(cur: Cursor, hospital_id: int, batch_id: int) -> dict | None:
    cur.execute(
        f"select {BATCH_COLUMNS} from public.batches b join public.items i on i.id = b.item_id"
        " where b.hospital_id = %s and b.id = %s",
        (hospital_id, batch_id),
    )
    return cur.fetchone()


def get_or_create_batch(
    cur: Cursor,
    hospital_id: int,
    item_id: int,
    batch_no: str,
    expiry_date: date,
    vendor_id: int | None,
) -> dict:
    cur.execute(
        "insert into public.batches (hospital_id, item_id, batch_no, expiry_date, vendor_id)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (hospital_id, item_id, batch_no) do nothing",
        (hospital_id, item_id, batch_no, expiry_date, vendor_id),
    )
    cur.execute(
        "select id, expiry_date from public.batches"
        " where hospital_id = %s and item_id = %s and batch_no = %s",
        (hospital_id, item_id, batch_no),
    )
    return cur.fetchone()


def block_batch(cur: Cursor, hospital_id: int, batch_id: int) -> None:
    cur.execute(
        "update public.batches set blocked = true where hospital_id = %s and id = %s",
        (hospital_id, batch_id),
    )


def mark_first_receipt(cur: Cursor, hospital_id: int, batch_ids: list[int]) -> None:
    cur.execute(
        "update public.batches set first_receipt_at = now()"
        " where hospital_id = %s and id = any(%s) and first_receipt_at is null",
        (hospital_id, batch_ids),
    )


def get_setting(cur: Cursor, hospital_id: int, key: str, default: Any) -> Any:
    cur.execute(
        "select value from public.hospital_settings where hospital_id = %s and key = %s",
        (hospital_id, key),
    )
    row = cur.fetchone()
    return default if row is None else row["value"]


def put_setting(cur: Cursor, hospital_id: int, key: str, value: Any) -> None:
    cur.execute(
        "insert into public.hospital_settings (hospital_id, key, value) values (%s, %s, %s::jsonb)"
        " on conflict (hospital_id, key) do update set value = excluded.value",
        (hospital_id, key, json.dumps(value)),
    )


def item_names(cur: Cursor, hospital_id: int, item_ids: list[int]) -> dict[int, dict]:
    cur.execute(
        "select id, name, formulary_status, controlled_class from public.items"
        " where hospital_id = %s and id = any(%s)",
        (hospital_id, item_ids),
    )
    return {r["id"]: r for r in cur.fetchall()}
