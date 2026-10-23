from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.api.v1.routes import user_tx
from app.api.v1.schemas import (
    ActionRequest,
    CaseCreate,
    CaseDetail,
    CaseSummary,
    FlowDefinition,
    ScanResult,
)
from app.core.auth import CurrentUser, current_user
from app.core.config import Settings, get_settings
from app.domain import expiry as expiry_service
from app.domain import flows as flows_service

router = APIRouter(prefix="/api/v1", tags=["flows"])


@router.get("/flows", response_model=list[FlowDefinition])
def list_flows(user: CurrentUser = Depends(current_user)):
    return flows_service.flow_definitions(user)


@router.get("/cases", response_model=list[CaseSummary])
async def list_cases(
    flow: int | None = Query(default=None, ge=1, le=5),
    node: str | None = None,
    open: bool | None = None,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(flows_service.list_cases, user, tx, flow, node, open)


@router.post("/cases", response_model=CaseDetail, status_code=201)
async def create_case(
    body: CaseCreate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(
        flows_service.create_case, user, tx, body.model_dump(), request.state.request_id
    )


@router.get("/cases/{case_id}", response_model=CaseDetail)
async def get_case(case_id: int, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(flows_service.get_case, user, tx, case_id)


@router.post("/cases/{case_id}/actions", response_model=CaseDetail)
async def take_action(
    case_id: int,
    body: ActionRequest,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
    settings: Settings = Depends(get_settings),
):
    return await run_in_threadpool(
        flows_service.take_action,
        user,
        tx,
        case_id,
        body.model_dump(),
        request.state.request_id,
        settings.demo_mode,
    )


@router.post("/expiry/scan", response_model=ScanResult)
async def expiry_scan(
    request: Request, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    return await run_in_threadpool(expiry_service.scan, user, tx, request.state.request_id)
