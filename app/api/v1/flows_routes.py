"""Flow definitions, cases and the expiry scan (feature 002)."""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from app.api.v1.routes import user_tx
from app.api.v1.schemas import (
    ActionRequest,
    CaseCreate,
    CaseDetail,
    CaseSummary,
    EdgeOut,
    FlowDefinition,
    NodeOut,
    ScanResult,
)
from app.core.auth import CurrentUser, current_user
from app.domain import expiry
from app.domain import flows as service
from app.flows.definitions import Edge, Flow

router = APIRouter(prefix="/api/v1", tags=["flows"])


def edge_out(edge: Edge) -> EdgeOut:
    return EdgeOut(
        id=edge.id,
        source=edge.source,
        target=edge.target,
        outcome=edge.outcome,
        approval=edge.approval,
        reason=edge.reason,
        inputs=list(edge.inputs),
    )


def flow_out(flow: Flow) -> FlowDefinition:
    return FlowDefinition(
        flow=flow.number,
        title=flow.title,
        starts=list(flow.starts),
        nodes=[NodeOut(key=n.key, label=n.label, kind=n.kind) for n in flow.nodes],
        edges=[edge_out(e) for e in flow.edges],
    )


def detail_out(case: dict) -> CaseDetail:
    return CaseDetail(**{**case, "actions": [edge_out(e) for e in case["actions"]]})


@router.get("/flows", response_model=list[FlowDefinition])
def list_flows(user: CurrentUser = Depends(current_user)):
    return [flow_out(f) for f in service.definitions(user)]


@router.get("/cases", response_model=list[CaseSummary])
async def list_cases(
    flow: int | None = None,
    node: str | None = None,
    open: bool | None = None,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(service.list_cases, user, tx, flow, node, open)


@router.post("/cases", response_model=CaseDetail, status_code=201)
async def create_case(
    body: CaseCreate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    case = await run_in_threadpool(
        service.create_case, user, tx, body.model_dump(), request.state.request_id
    )
    return detail_out(case)


@router.get("/cases/{case_id}", response_model=CaseDetail)
async def get_case(case_id: int, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return detail_out(await run_in_threadpool(service.get_case, user, tx, case_id))


@router.post("/cases/{case_id}/actions", response_model=CaseDetail)
async def take_action(
    case_id: int,
    body: ActionRequest,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    case = await run_in_threadpool(
        service.take_action,
        user,
        tx,
        case_id,
        body.edge,
        body.note,
        body.input,
        request.state.request_id,
    )
    return detail_out(case)


@router.post("/expiry/scan", response_model=ScanResult)
async def expiry_scan(
    request: Request, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    ids = await run_in_threadpool(expiry.scan, user, tx, request.state.request_id)
    return ScanResult(created=len(ids), case_ids=ids)
