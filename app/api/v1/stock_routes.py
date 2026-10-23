from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.api.v1.routes import user_tx
from app.api.v1.schemas import Balance, Batch, BatchTrace, Dashboard, Location, Vendor
from app.core.auth import CurrentUser, current_user
from app.domain import inventory

router = APIRouter(prefix="/api/v1", tags=["stock"])


@router.get("/locations", response_model=list[Location], tags=["master-data"])
async def list_locations(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(inventory.locations, user, tx)


@router.get("/vendors", response_model=list[Vendor], tags=["master-data"])
async def list_vendors(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(inventory.vendors, user, tx)


@router.get("/batches", response_model=list[Batch], tags=["master-data"])
async def list_batches(
    item_id: int | None = None, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    return await run_in_threadpool(inventory.batches, user, tx, item_id)


@router.get("/stock/balances", response_model=list[Balance])
async def list_balances(
    location_id: int | None = None,
    item_id: int | None = None,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(inventory.balances, user, tx, location_id, item_id)


@router.get("/batches/{batch_id}/trace", response_model=BatchTrace)
async def batch_trace(
    batch_id: int, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)
):
    return await run_in_threadpool(inventory.batch_trace, user, tx, batch_id)


@router.get("/dashboard", response_model=Dashboard)
async def dashboard(user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(inventory.dashboard, user, tx)
