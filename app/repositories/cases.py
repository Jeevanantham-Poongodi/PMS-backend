"""SQL for flow cases, their lines, their append-only history and NDPS register entries."""

from decimal import Decimal
from typing import Any

from psycopg import Cursor
from psycopg.types.json import Jsonb

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
    closed: bool = False,
) -> dict:
    cur.execute(
        "insert into public.flow_cases (hospital_id, flow, node, title, data, parent_case_id,"
        f" closed) values (%s, %s, %s, %s, %s, %s, %s) returning {CASE_COLUMNS}",
        (hospital_id, flow, node, title, Jsonb(data), parent_case_id, closed),
    )
    return cur.fetchone()


def get_case(cur: Cursor, hospital_id: int, case_id: int, for_update: bool = False) -> dict | None:
    lock = " for update" if for_update else ""
    cur.execute(
        f"select {CASE_COLUMNS} from public.flow_cases where hospital_id = %s and id = %s{lock}",
        (hospital_id, case_id),
    )
    return cur.fetchone()


def list_cases(
    cur: Cursor, hospital_id: int, flow: int | None, node: str | None, open_: bool | None
) -> list[dict]:
    sql = f"select {CASE_COLUMNS} from public.flow_cases where hospital_id = %s"
    params: list[Any] = [hospital_id]
    if flow is not None:
        sql += " and flow = %s"
        params.append(flow)
    if node:
        sql += " and node = %s"
        params.append(node)
    if open_ is not None:
        sql += " and closed = %s"
        params.append(not open_)
    cur.execute(sql + " order by updated_at desc, id desc limit 500", params)
    return cur.fetchall()


def update_case(
    cur: Cursor, hospital_id: int, case_id: int, node: str, closed: bool, data: dict
) -> None:
    cur.execute(
        "update public.flow_cases set node = %s, closed = %s, data = %s, updated_at = now()"
        " where hospital_id = %s and id = %s",
        (node, closed, Jsonb(data), hospital_id, case_id),
    )


def child_case_ids(cur: Cursor, hospital_id: int, case_id: int) -> list[int]:
    cur.execute(
        "select id from public.flow_cases where hospital_id = %s and parent_case_id = %s"
        " order by id",
        (hospital_id, case_id),
    )
    return [row["id"] for row in cur.fetchall()]


def open_expiry_case_exists(
    cur: Cursor, hospital_id: int, batch_id: int, location_id: int
) -> bool:
    cur.execute(
        "select 1 from public.flow_cases where hospital_id = %s and flow = 4 and not closed"
        " and (data->>'batch_id')::bigint = %s and (data->>'location_id')::bigint = %s limit 1",
        (hospital_id, batch_id, location_id),
    )
    return cur.fetchone() is not None


def open_cases_by_node(cur: Cursor, hospital_id: int) -> list[dict]:
    cur.execute(
        "select flow, node, count(*) as count from public.flow_cases"
        " where hospital_id = %s and not closed group by flow, node order by flow, node",
        (hospital_id,),
    )
    return cur.fetchall()


# Lines ------------------------------------------------------------------------------------------

LINE_SELECT = (
    "select fl.id, fl.item_id, i.name as item_name, fl.batch_id, b.batch_no, b.expiry_date,"
    " fl.location_id, l.name as location_name, fl.qty_requested, fl.qty_approved"
    " from public.flow_case_lines fl"
    " join public.items i on i.id = fl.item_id"
    " left join public.batches b on b.id = fl.batch_id"
    " left join public.locations l on l.id = fl.location_id"
)


def insert_line(
    cur: Cursor,
    hospital_id: int,
    case_id: int,
    item_id: int,
    qty: Decimal,
    batch_id: int | None = None,
    location_id: int | None = None,
    qty_approved: Decimal | None = None,
) -> None:
    cur.execute(
        "insert into public.flow_case_lines (hospital_id, case_id, item_id, batch_id,"
        " location_id, qty_requested, qty_approved) values (%s, %s, %s, %s, %s, %s, %s)",
        (hospital_id, case_id, item_id, batch_id, location_id, qty, qty_approved),
    )


def get_lines(cur: Cursor, hospital_id: int, case_id: int) -> list[dict]:
    cur.execute(
        LINE_SELECT + " where fl.hospital_id = %s and fl.case_id = %s order by fl.id",
        (hospital_id, case_id),
    )
    return cur.fetchall()


def update_line(cur: Cursor, hospital_id: int, line_id: int, changes: dict) -> None:
    assignments = ", ".join(f"{column} = %s" for column in changes)
    cur.execute(
        f"update public.flow_case_lines set {assignments} where hospital_id = %s and id = %s",
        (*changes.values(), hospital_id, line_id),
    )


# History ----------------------------------------------------------------------------------------


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
        " values (%s, %s, %s, %s, %s, %s, %s)",
        (
            hospital_id,
            case_id,
            from_node,
            outcome,
            to_node,
            note,
            Jsonb(input_) if input_ is not None else None,
        ),
    )


def get_events(cur: Cursor, hospital_id: int, case_id: int) -> list[dict]:
    cur.execute(
        "select e.id, e.from_node, e.outcome, e.to_node,"
        " coalesce(m.display_name, 'Unknown user') as actor_name, e.note, e.created_at"
        " from public.flow_events e"
        " left join public.hospital_members m"
        "   on m.hospital_id = e.hospital_id and m.user_id = e.actor"
        " where e.hospital_id = %s and e.case_id = %s order by e.id",
        (hospital_id, case_id),
    )
    return cur.fetchall()


# NDPS register (FR-023) -------------------------------------------------------------------------


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
        " values (%s, %s, %s, %s, %s, %s::uuid, %s::uuid)",
        (hospital_id, case_id, item_id, qty, patient_ref, signoff_1, signoff_2),
    )


def ndps_item_ids(cur: Cursor, hospital_id: int, case_id: int) -> set[int]:
    cur.execute(
        "select distinct item_id from public.ndps_register where hospital_id = %s and case_id = %s",
        (hospital_id, case_id),
    )
    return {row["item_id"] for row in cur.fetchall()}
