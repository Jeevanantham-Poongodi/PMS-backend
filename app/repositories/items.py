"""SQL for the item master. Runs inside a user transaction, so RLS also filters every query."""

from typing import Any

from psycopg import Cursor

COLUMNS = "id, name, base_unit, formulary_status, controlled_class, active, created_at, updated_at"


def list_items(cur: Cursor, hospital_id: int, q: str | None, active: bool | None) -> list[dict]:
    sql = f"select {COLUMNS} from public.items where hospital_id = %s"
    params: list[Any] = [hospital_id]
    if q:
        sql += " and name ilike %s"
        params.append(f"%{q}%")
    if active is not None:
        sql += " and active = %s"
        params.append(active)
    cur.execute(sql + " order by lower(name) limit 500", params)
    return cur.fetchall()


def get_item(cur: Cursor, hospital_id: int, item_id: int, for_update: bool = False) -> dict | None:
    lock = " for update" if for_update else ""
    cur.execute(
        f"select {COLUMNS} from public.items where hospital_id = %s and id = %s{lock}",
        (hospital_id, item_id),
    )
    return cur.fetchone()


def insert_item(cur: Cursor, hospital_id: int, data: dict) -> dict:
    cur.execute(
        "insert into public.items"
        " (hospital_id, name, base_unit, formulary_status, controlled_class)"
        f" values (%s, %s, %s, %s, %s) returning {COLUMNS}",
        (
            hospital_id,
            data["name"],
            data["base_unit"],
            data["formulary_status"],
            data["controlled_class"],
        ),
    )
    return cur.fetchone()


def update_item(cur: Cursor, hospital_id: int, item_id: int, changes: dict) -> dict:
    assignments = ", ".join(f"{column} = %s" for column in changes)
    cur.execute(
        f"update public.items set {assignments}, updated_at = now()"
        f" where hospital_id = %s and id = %s returning {COLUMNS}",
        (*changes.values(), hospital_id, item_id),
    )
    return cur.fetchone()
