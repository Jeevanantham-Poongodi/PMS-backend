"""The case engine (FR-001 to FR-005, research R1 and R5).

A case is always at one node of its flow. Only the outgoing arrows of that node that the data
allows are offered; taking one runs its effect, records the move in the append-only history and
writes an audit entry, all in one transaction.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.errors import AppError
from app.domain import stock
from app.domain.effects import EFFECTS, GUARDS, Ctx
from app.flows.definitions import FLOWS, Edge, get_flow, is_end, outgoing
from app.repositories import audit
from app.repositories import cases as repo
from app.repositories import masters as masters_repo
from app.repositories import stock as stock_repo

TxFactory = Callable[[], AbstractContextManager[Cursor]]

WARD_KINDS = ("ward", "ot", "icu")
# Case details each flow's start form may set; anything else in `data` is ignored.
START_DATA = {
    1: ("trigger", "vendor_id"),
    2: ("ward_location_id",),
    3: ("order_type", "patient_ref", "prescriber"),
    5: ("batch_id", "source"),
}


def invalid(message: str, field: str | None = None) -> AppError:
    details = [{"field": field, "message": message}] if field else None
    return AppError(422, "VALIDATION_FAILED", message, details)


# Definitions --------------------------------------------------------------------------------------


def edge_dict(edge: Edge) -> dict:
    return {
        "id": edge.id,
        "from": edge.source,
        "to": edge.target,
        "outcome": edge.outcome,
        "approval": edge.approval,
        "reason": edge.reason,
        "inputs": list(edge.inputs),
    }


def flow_definitions(user: CurrentUser) -> list[dict]:
    user.require("flows.read")
    return [
        {
            "flow": f.flow,
            "title": f.title,
            "starts": list(f.starts),
            "nodes": [{"key": n.key, "label": n.label, "kind": n.kind} for n in f.nodes],
            "edges": [edge_dict(e) for e in f.edges],
        }
        for f in FLOWS.values()
    ]


def node_label(flow: int, node: str) -> str:
    found = get_flow(flow).node(node)
    return found.label if found else node


# Reading ------------------------------------------------------------------------------------------


def _summary(case: dict) -> dict:
    return {
        "id": case["id"],
        "flow": case["flow"],
        "node": case["node"],
        "node_label": node_label(case["flow"], case["node"]),
        "title": case["title"],
        "closed": case["closed"],
        "parent_case_id": case["parent_case_id"],
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
    }


def _ctx(cur: Cursor, user: CurrentUser, case: dict, demo_mode: bool = False) -> Ctx:
    lines = repo.get_lines(cur, user.hospital_id, case["id"])
    return Ctx(cur=cur, user=user, case=case, lines=lines, data=dict(case["data"] or {}),
               demo_mode=demo_mode)


def available_actions(ctx: Ctx) -> list[Edge]:
    """FR-002: the outgoing arrows of the current node that the data allows (R2)."""
    if ctx.case["closed"]:
        return []
    return [
        e for e in outgoing(ctx.case["flow"], ctx.case["node"])
        if e.guard is None or GUARDS[e.guard](ctx)
    ]


def _detail(cur: Cursor, user: CurrentUser, case_id: int) -> dict:
    case = repo.get_case(cur, user.hospital_id, case_id)
    if case is None:
        raise AppError(404, "NOT_FOUND", "Case not found.")
    ctx = _ctx(cur, user, case)
    return {
        **_summary(case),
        "data": case["data"] or {},
        "lines": ctx.lines,
        "events": repo.get_events(cur, user.hospital_id, case_id),
        "actions": [edge_dict(e) for e in available_actions(ctx)],
        "child_case_ids": repo.child_case_ids(cur, user.hospital_id, case_id),
    }


def list_cases(
    user: CurrentUser, tx: TxFactory, flow: int | None, node: str | None, open_: bool | None
) -> list[dict]:
    user.require("flows.read")
    with tx() as cur:
        return [_summary(c) for c in repo.list_cases(cur, user.hospital_id, flow, node, open_)]


def get_case(user: CurrentUser, tx: TxFactory, case_id: int) -> dict:
    user.require("flows.read")
    with tx() as cur:
        return _detail(cur, user, case_id)


# Raising a case -----------------------------------------------------------------------------------


def open_case(
    cur: Cursor,
    user: CurrentUser,
    *,
    flow: int,
    start: str,
    title: str,
    data: dict,
    lines: list[dict],
    parent_case_id: int | None = None,
    request_id: str | None = None,
) -> int:
    """Inserts a case at a start node with its lines, first history entry and audit entry.
    Also used by effects that open linked cases."""
    case = repo.insert_case(cur, user.hospital_id, flow, start, title, data, parent_case_id)
    for line in lines:
        repo.insert_line(
            cur,
            user.hospital_id,
            case["id"],
            line["item_id"],
            stock.to_qty(line["qty"]),
            line.get("batch_id"),
            line.get("location_id"),
            line.get("qty_approved"),
        )
    repo.insert_event(cur, user.hospital_id, case["id"], None, None, start, None, None)
    audit.append(
        cur,
        hospital_id=user.hospital_id,
        action="create",
        entity_type="flow_case",
        entity_id=str(case["id"]),
        after={"flow": flow, "node": start, "title": title, "parent_case_id": parent_case_id},
        request_id=request_id,
    )
    return case["id"]


def _check_start(cur: Cursor, user: CurrentUser, body: dict) -> tuple[dict, list[dict]]:
    """Validates the start form of each flow; returns the case data and lines to store."""
    flow, start = body["flow"], body["start"]
    if flow == 4:
        raise invalid("Expiry cases are raised by the daily expiry scan.", "flow")
    if start not in get_flow(flow).starts:
        raise invalid("That is not a start step of this flow.", "start")
    given = body.get("data") or {}
    data = {k: given[k] for k in START_DATA[flow] if given.get(k) not in (None, "")}
    lines = body.get("lines") or []
    hid = user.hospital_id

    if flow == 5:
        if not data.get("batch_id"):
            raise invalid("Choose the recalled batch.", "data.batch_id")
        batch = masters_repo.get_batch(cur, hid, int(data["batch_id"]))
        if batch is None:
            raise invalid("Unknown batch.", "data.batch_id")
        if data.get("source") not in ("regulator", "vendor"):
            raise invalid("Say whether the notice came from the regulator or the vendor.",
                          "data.source")
        data["batch_id"] = batch["id"]
        held = sum((h["qty"] for h in stock_repo.batch_holdings(cur, hid, batch["id"])),
                   Decimal(0))
        return data, [{"item_id": batch["item_id"], "batch_id": batch["id"], "qty": held}]

    if not lines:
        raise invalid("Add at least one line.", "lines")
    items = masters_repo.get_items(cur, hid, [ln["item_id"] for ln in lines])
    for line in lines:
        item = items.get(line["item_id"])
        if item is None or not item["active"]:
            raise invalid("Every line needs an active item.", "lines")

    if flow == 1:
        if start == "f1_need" and data.get("trigger") not in ("reorder_level", "department_need"):
            raise invalid("Choose what triggered the requisition.", "data.trigger")
        if start == "f1_emergency":
            if not data.get("vendor_id") or masters_repo.get_vendor(
                cur, hid, int(data["vendor_id"])
            ) is None:
                raise invalid("Choose the vendor the emergency purchase came from.",
                              "data.vendor_id")
            data["vendor_id"] = int(data["vendor_id"])
    elif flow == 2:
        ward = masters_repo.get_location(cur, hid, int(data.get("ward_location_id") or 0))
        if ward is None or ward["kind"] not in WARD_KINDS:
            raise invalid("Choose the ward, OT or ICU raising the indent.",
                          "data.ward_location_id")
        data["ward_location_id"] = ward["id"]
    elif flow == 3:
        if data.get("order_type") not in ("IP", "OP"):
            raise invalid("Choose IP or OP.", "data.order_type")
        for key in ("patient_ref", "prescriber"):
            data[key] = str(data.get(key) or "").strip()
            if not data[key]:
                raise invalid(f"Enter the {key.replace('_', ' ')}.", f"data.{key}")
    return data, lines


def create_case(user: CurrentUser, tx: TxFactory, body: dict, request_id: str) -> dict:
    user.require("flows.write")
    with tx() as cur:
        data, lines = _check_start(cur, user, body)
        case_id = open_case(
            cur,
            user,
            flow=body["flow"],
            start=body["start"],
            title=body["title"],
            data=data,
            lines=lines,
            request_id=request_id,
        )
        return _detail(cur, user, case_id)


# Moving a case ------------------------------------------------------------------------------------


def move_case(
    cur: Cursor,
    user: CurrentUser,
    case_id: int,
    edge_id: str,
    note: str | None,
    input_: dict | None,
    request_id: str | None,
    demo_mode: bool,
) -> None:
    """Takes one arrow inside an open transaction. Used by the API and the demo-data loader."""
    case = repo.get_case(cur, user.hospital_id, case_id, for_update=True)
    if case is None:
        raise AppError(404, "NOT_FOUND", "Case not found.")
    if case["closed"]:
        raise AppError(409, "CASE_CLOSED", "This case is closed.")
    ctx = _ctx(cur, user, case, demo_mode)
    edge = next((e for e in outgoing(case["flow"], case["node"]) if e.id == edge_id), None)
    if edge is None or edge not in available_actions(ctx):
        raise AppError(
            409, "INVALID_TRANSITION", "That step is not available from where this case is now.",
            {"node": case["node"], "edge": edge_id},
        )
    note = (note or "").strip() or None
    if edge.reason and not note:
        raise invalid("A reason is required for this outcome.", "note")
    if edge.approval:
        user.require("flows.approve")
        if not demo_mode and str(case["raised_by"]) == user.user_id:
            raise AppError(
                403, "SELF_APPROVAL_FORBIDDEN", "The person who raised a case cannot approve it."
            )

    ctx.edge, ctx.input, ctx.note, ctx.request_id = edge, input_ or {}, note, request_id
    if edge.effect:
        EFFECTS[edge.effect](ctx)

    closed = is_end(case["flow"], edge.target)
    repo.update_case(cur, user.hospital_id, case_id, edge.target, closed, ctx.data)
    repo.insert_event(
        cur, user.hospital_id, case_id, case["node"], edge.outcome, edge.target, note, input_
    )
    audit.append(
        cur,
        hospital_id=user.hospital_id,
        action="move",
        entity_type="flow_case",
        entity_id=str(case_id),
        before={"node": case["node"]},
        after={"node": edge.target, "outcome": edge.outcome},
        reason=note if edge.reason else None,
        request_id=request_id,
    )


def take_action(
    user: CurrentUser,
    tx: TxFactory,
    case_id: int,
    body: dict[str, Any],
    request_id: str,
    demo_mode: bool,
) -> dict:
    user.require("flows.write")
    with tx() as cur:
        move_case(
            cur, user, case_id, body["edge"], body.get("note"), body.get("input"), request_id,
            demo_mode,
        )
        return _detail(cur, user, case_id)
