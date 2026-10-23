"""SQL for flow cases, their lines and their append-only history. Runs inside a user
transaction, so RLS also filters every query."""

import json
from decimal import Decimal
from typing import Any

from psycopg import Cursor

CASE_COLUMNS = (
    "id, flow, node, title, data, parent_case_id, closed, raised_by, created_at, updated_at"
)


def insert_case(
    cur: Cursor,
    hospital_id: int,
    flow: int,
    node: str,
    title: str,
    data: dict,
    parent_case_id: int | None,
) -> dict:
    cur.execute(
        "insert into public.flow_cases (hospital_id, flow, node, title, data, parent_case_id)"
        f" values (%s, %s, %s, %s, %s::jsonb, %s) returning {CASE_COLUMNS}",
        (hospital_id, flow, node, title, json.dumps(data), parent_case_id),
    )
    return cur.fetchone()


def insert_line(
    cur: Cursor,
    hospital_id: int,
    case_id: int,
    item_id: int,
    qty: Decimal,
    batch_id: int | None = None,
    location_id: int | None = None,
) -> None:
    cur.execute(
        "insert into public.flow_case_lines"
        " (hospital_id, case_id, item_id, batch_id, location_id, qty_requested)"
        " values (%s, %s, %s, %s, %s, %s)",
        (hospital_id, case_id, item_id, batch_id, location_id, qty),
    )


def get_case(cur: Cursor, hospital_id: int, case_id: int, for_update: bool = False) -> dict | None:
    lock = " for update" if for_update else ""
    cur.execute(
        f"select {CASE_COLUMNS} from public.flow_cases where hospital_id = %s and id = %s{lock}",
        (hospital_id, case_id),
    )
    return cur.fetchone()


def list_cases(
    cur: Cursor, hospital_id: int, flow: int | None, node: str | None, open_only: bool | None
) -> list[dict]:
    sql = f"select {CASE_COLUMNS} from public.flow_cases where hospital_id = %s"
    params: list[Any] = [hospital_id]
    if flow is not None:
        sql += " and flow = %s"
        params.append(flow)
    if node:
        sql += " and node = %s"
        params.append(node)
    if open_only is not None:
        sql += " and closed = %s"
        params.append(not open_only)
    cur.execute(sql + " order by updated_at desc, id desc limit 500", params)
    return cur.fetchall()


def update_case(cur: Cursor, hospital_id: int, case_id: int, node: str, closed: bool, data: dict):
    cur.execute(
        "update public.flow_cases set node = %s, closed = %s, data = %s::jsonb, updated_at = now()"
        " where hospital_id = %s and id = %s",
        (node, closed, json.dumps(data), hospital_id, case_id),
    )


def get_lines(cur: Cursor, hospital_id: int, case_id: int) -> list[dict]:
    cur.execute(
        "select cl.id, cl.item_id, i.name as item_name, i.formulary_status, i.controlled_class,"
        " cl.batch_id, b.batch_no, b.expiry_date, cl.location_id, l.name as location_name,"
        " cl.qty_requested, cl.qty_approved"
        " from public.flow_case_lines cl"
        " join public.items i on i.id = cl.item_id"
        " left join public.batches b on b.id = cl.batch_id"
        " left join public.locations l on l.id = cl.location_id"
        " where cl.hospital_id = %s and cl.case_id = %s order by cl.id",
        (hospital_id, case_id),
    )
    return cur.fetchall()


def update_line(cur: Cursor, hospital_id: int, line_id: int, **changes: Any) -> None:
    assignments = ", ".join(f"{column} = %s" for column in changes)
    cur.execute(
        f"update public.flow_case_lines set {assignments} where hospital_id = %s and id = %s",
        (*changes.values(), hospital_id, line_id),
    )


def insert_event(
    cur: Cursor,
    hospital_id: int,
    case_id: int,
    from_node: str | None,
    outcome: str | None,
    to_node: str,
    note: str | None,
    input_: dict | None,
) -> None:
    cur.execute(
        "insert into public.flow_events"
        " (hospital_id, case_id, from_node, outcome, to_node, note, input)"
        " values (%s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            hospital_id,
            case_id,
            from_node,
            outcome,
            to_node,
            note,
            json.dumps(input_) if input_ else None,
        ),
    )


def get_events(cur: Cursor, hospital_id: int, case_id: int) -> list[dict]:
    cur.execute(
        "select e.id, e.from_node, e.outcome, e.to_node, e.note, e.created_at,"
        " coalesce(m.display_name, 'Unknown user') as actor_name"
        " from public.flow_events e"
        " left join public.hospital_members m"
        "   on m.hospital_id = e.hospital_id and m.user_id = e.actor"
        " where e.hospital_id = %s and e.case_id = %s order by e.id",
        (hospital_id, case_id),
    )
    return cur.fetchall()


def child_case_ids(cur: Cursor, hospital_id: int, case_id: int) -> list[int]:
    cur.execute(
        "select id from public.flow_cases where hospital_id = %s and parent_case_id = %s"
        " order by id",
        (hospital_id, case_id),
    )
    return [r["id"] for r in cur.fetchall()]


def open_expiry_case_exists(cur: Cursor, hospital_id: int, batch_id: int, location_id: int):
    cur.execute(
        "select 1 from public.flow_cases where hospital_id = %s and flow = 4 and not closed"
        " and (data->>'batch_id')::bigint = %s and (data->>'location_id')::bigint = %s",
        (hospital_id, batch_id, location_id),
    )
    return cur.fetchone() is not None


def ndps_entries(cur: Cursor, hospital_id: int, case_id: int) -> list[dict]:
    cur.execute(
        "select item_id, qty from public.ndps_register where hospital_id = %s and case_id = %s",
        (hospital_id, case_id),
    )
    return cur.fetchall()


def insert_ndps(
    cur: Cursor,
    hospital_id: int,
    case_id: int,
    item_id: int,
    qty: Decimal,
    patient_ref: str,
    signoff_1: str,
    signoff_2: str,
) -> None:
    cur.execute(
        "insert into public.ndps_register"
        " (hospital_id, case_id, item_id, qty, patient_ref, signoff_1, signoff_2)"
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (hospital_id, case_id, item_id, qty, patient_ref, signoff_1, signoff_2),
    )


def counts_by_node(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select flow, node, count(*) as count from public.flow_cases"
        " where hospital_id = %s and not closed group by flow, node order by flow, node",
        (hospital_id,),
    )
    return cur.fetchall()
