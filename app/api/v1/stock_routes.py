"""Masters, stock balances, batch trace and the dashboard (feature 002)."""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.api.v1.routes import user_tx
from app.api.v1.schemas import Balance, Batch, BatchTrace, Dashboard, Location, Vendor
from app.core.auth import CurrentUser, current_user
from app.domain import overview

router = APIRouter(prefix="/api/v1", tags=["stock"])


@router.get("/locations", response_model=list[Location])
async def list_locations(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(overview.locations, user, tx)


@router.get("/vendors", response_model=list[Vendor])
async def list_vendors(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(overview.vendors, user, tx)


@router.get("/batches", response_model=list[Batch])
async def list_batches(
    item_id: int | None = None, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    return await run_in_threadpool(overview.batches, user, tx, item_id)


@router.get("/batches/{batch_id}/trace", response_model=BatchTrace)
async def batch_trace(
    batch_id: int, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    return await run_in_threadpool(overview.trace, user, tx, batch_id)


@router.get("/stock/balances", response_model=list[Balance])
async def stock_balances(
    location_id: int | None = None,
    item_id: int | None = None,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(overview.balances, user, tx, location_id, item_id)


@router.get("/dashboard", response_model=Dashboard)
async def dashboard(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(overview.dashboard, user, tx)
