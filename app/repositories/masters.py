"""SQL for locations, vendors, batches and hospital settings. Runs inside a user transaction."""

from datetime import date
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb


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


def location_of_kind(cur: Cursor, hospital_id: int, kind: str) -> dict | None:
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


def get_vendor(cur: Cursor, hospital_id: int, vendor_id: int) -> dict | None:
    cur.execute(
        "select id, name from public.vendors where hospital_id = %s and id = %s",
        (hospital_id, vendor_id),
    )
    return cur.fetchone()


BATCH_COLUMNS = (
    "b.id, b.item_id, i.name as item_name, b.batch_no, b.expiry_date, b.vendor_id, b.blocked"
)


def list_batches(cur: Cursor, hospital_id: int, item_id: int | None = None) -> list[dict]:
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
        "select id, expiry_date from public.batches"
        " where hospital_id = %s and item_id = %s and batch_no = %s",
        (hospital_id, item_id, batch_no),
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        "insert into public.batches (hospital_id, item_id, batch_no, expiry_date, vendor_id)"
        " values (%s, %s, %s, %s, %s) returning id, expiry_date",
        (hospital_id, item_id, batch_no, expiry_date, vendor_id),
    )
    return cur.fetchone()


def block_batch(cur: Cursor, hospital_id: int, batch_id: int) -> None:
    cur.execute(
        "update public.batches set blocked = true where hospital_id = %s and id = %s",
        (hospital_id, batch_id),
    )


def get_items(cur: Cursor, hospital_id: int, item_ids: list[int]) -> dict[int, dict]:
    if not item_ids:
        return {}
    cur.execute(
        "select id, name, formulary_status, controlled_class, active from public.items"
        " where hospital_id = %s and id = any(%s)",
        (hospital_id, list(item_ids)),
    )
    return {row["id"]: row for row in cur.fetchall()}


def get_setting(cur: Cursor, hospital_id: int, key: str, default: Any = None) -> Any:
    cur.execute(
        "select value from public.hospital_settings where hospital_id = %s and key = %s",
        (hospital_id, key),
    )
    row = cur.fetchone()
    return default if row is None else row["value"]


def set_setting(cur: Cursor, hospital_id: int, key: str, value: Any) -> None:
    cur.execute(
        "insert into public.hospital_settings (hospital_id, key, value) values (%s, %s, %s)"
        " on conflict (hospital_id, key) do update set value = excluded.value",
        (hospital_id, key, Jsonb(value)),
    )


def member_exists(cur: Cursor, hospital_id: int, user_id: str) -> bool:
    cur.execute(
        "select 1 from public.hospital_members"
        " where hospital_id = %s and user_id = %s::uuid and active",
        (hospital_id, user_id),
    )
    return cur.fetchone() is not None
