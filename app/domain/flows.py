"""The case engine (FR-001–FR-005): a case is always at one node of its flow, can only take an
outgoing arrow of that node, and every move is recorded with its audit entry in one transaction.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain.effects import EFFECTS, GUARDS, Ctx
from app.flows.definitions import FLOWS, Edge, Flow, get_flow
from app.repositories import audit
from app.repositories import cases as repo

TxFactory = Callable[[], AbstractContextManager[Cursor]]


def definitions(user: CurrentUser) -> list[Flow]:
    user.require("flows.read")
    return list(FLOWS.values())


def _flow(number: int) -> Flow:
    flow = get_flow(number)
    if flow is None:
        raise AppError(404, "NOT_FOUND", "No such flow.")
    return flow


def _ctx(cur: Cursor, user: CurrentUser, case: dict, edge: Edge | None = None, **kw: Any) -> Ctx:
    return Ctx(
        cur=cur,
        user=user,
        case=case,
        lines=repo.get_lines(cur, user.hospital_id, case["id"]),
        data=dict(case["data"] or {}),
        edge=edge,  # type: ignore[arg-type]
        input=kw.get("input") or {},
        note=kw.get("note"),
        request_id=kw.get("request_id"),
    )


def available_edges(ctx: Ctx) -> list[Edge]:
    if ctx.case["closed"]:
        return []
    flow = _flow(ctx.case["flow"])
    return [e for e in flow.outgoing(ctx.case["node"]) if e.guard is None or GUARDS[e.guard](ctx)]


def _detail(cur: Cursor, user: CurrentUser, case: dict) -> dict:
    ctx = _ctx(cur, user, case)
    flow = _flow(case["flow"])
    return {
        **case,
        "node_label": flow.node(case["node"]).label,
        "lines": ctx.lines,
        "events": repo.get_events(cur, user.hospital_id, case["id"]),
        "actions": available_edges(ctx),
        "child_case_ids": repo.child_case_ids(cur, user.hospital_id, case["id"]),
    }


def summary(case: dict) -> dict:
    return {**case, "node_label": _flow(case["flow"]).node(case["node"]).label}


def list_cases(
    user: CurrentUser, tx: TxFactory, flow: int | None, node: str | None, open_only: bool | None
) -> list[dict]:
    user.require("flows.read")
    with tx() as cur:
        return [summary(c) for c in repo.list_cases(cur, user.hospital_id, flow, node, open_only)]


def get_case(user: CurrentUser, tx: TxFactory, case_id: int) -> dict:
    user.require("flows.read")
    with tx() as cur:
        case = repo.get_case(cur, user.hospital_id, case_id)
        if case is None:
            raise AppError(404, "NOT_FOUND", "Case not found.")
        return _detail(cur, user, case)


def open_case(
    cur: Cursor,
    user: CurrentUser,
    flow_number: int,
    start: str,
    title: str,
    data: dict,
    lines: list[dict],
    request_id: str | None,
) -> dict:
    flow = _flow(flow_number)
    if start not in flow.starts:
        raise AppError(422, "VALIDATION_FAILED", f"{start} is not a start of flow {flow_number}.")
    case = repo.insert_case(cur, user.hospital_id, flow_number, start, title, data, None)
    for line in lines:
        qty = Decimal(str(line["qty"]))
        if qty <= 0:
            raise AppError(422, "VALIDATION_FAILED", "Quantities must be greater than zero.")
        repo.insert_line(
            cur,
            user.hospital_id,
            case["id"],
            int(line["item_id"]),
            qty,
            line.get("batch_id"),
            line.get("location_id"),
        )
    repo.insert_event(cur, user.hospital_id, case["id"], None, None, start, None, None)
    audit.append(
        cur,
        hospital_id=user.hospital_id,
        action="create",
        entity_type="flow_case",
        entity_id=str(case["id"]),
        after={"flow": flow_number, "node": start, "title": title},
        request_id=request_id,
    )
    return case


def create_case(user: CurrentUser, tx: TxFactory, body: dict, request_id: str | None) -> dict:
    user.require("flows.write")
    with tx() as cur:
        case = open_case(
            cur,
            user,
            body["flow"],
            body["start"],
            body["title"],
            body.get("data") or {},
            body.get("lines") or [],
            request_id,
        )
        return _detail(cur, user, case)


def move(
    cur: Cursor,
    user: CurrentUser,
    case_id: int,
    edge_id: str,
    note: str | None,
    input_: dict | None,
    request_id: str | None,
) -> dict:
    """Takes one arrow. Raises before anything is written if the move is not allowed."""
    case = repo.get_case(cur, user.hospital_id, case_id, for_update=True)
    if case is None:
        raise AppError(404, "NOT_FOUND", "Case not found.")
    if case["closed"]:
        raise AppError(409, "CASE_CLOSED", "This case is closed.")
    flow = _flow(case["flow"])
    edge = flow.edge(edge_id)
    note = (note or "").strip() or None
    ctx = _ctx(cur, user, case, edge, input=input_, note=note, request_id=request_id)
    if edge is None or edge not in available_edges(ctx):
        raise AppError(
            409, "INVALID_TRANSITION", "That outcome is not available from the case's current step."
        )
    if edge.reason and not note:
        raise AppError(422, "VALIDATION_FAILED", "A reason is required for this outcome.")
    if edge.approval:
        user.require("flows.approve")
        if str(case["raised_by"]) == str(user.user_id) and not get_settings().demo_mode:
            raise AppError(
                403, "SELF_APPROVAL_FORBIDDEN", "The person who raised this case cannot approve it."
            )
    if edge.effect:
        EFFECTS[edge.effect](ctx)
    closed = flow.node(edge.target).kind == "end"
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
        reason=note,
        request_id=request_id,
    )
    return repo.get_case(cur, user.hospital_id, case_id)


def take_action(
    user: CurrentUser,
    tx: TxFactory,
    case_id: int,
    edge_id: str,
    note: str | None,
    input_: dict | None,
    request_id: str | None,
) -> dict:
    user.require("flows.write")
    with tx() as cur:
        case = move(cur, user, case_id, edge_id, note, input_, request_id)
        return _detail(cur, user, case)
