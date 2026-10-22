from functools import partial

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from app.api.v1.schemas import Health, Item, ItemCreate, ItemUpdate, Me
from app.core import db
from app.core.auth import CurrentUser, current_user
from app.core.config import get_settings
from app.domain import items as items_service

router = APIRouter(prefix="/api/v1")


def user_tx(user: CurrentUser = Depends(current_user)):
    return partial(db.user_transaction, user.claims)


@router.get("/health", response_model=Health, tags=["system"])
def health() -> Health:
    if not get_settings().database_url:
        return Health(status="degraded", database="not_configured")
    try:
        ok = db.ping()
    except Exception:
        ok = False
    return Health(status="ok" if ok else "degraded", database="ok" if ok else "unreachable")


@router.get("/me", response_model=Me, tags=["access"])
def me(user: CurrentUser = Depends(current_user)) -> Me:
    return Me(
        user_id=user.user_id,
        hospital_id=user.hospital_id,
        display_name=user.display_name,
        roles=sorted(user.roles),
        permissions=sorted(user.permissions),
        mfa_satisfied=user.mfa_satisfied,
    )


@router.get("/items", response_model=list[Item], tags=["master-data"])
async def list_items(
    q: str | None = None,
    active: bool | None = None,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(items_service.list_items, user, tx, q, active)


@router.get("/items/{item_id}", response_model=Item, tags=["master-data"])
async def get_item(item_id: int, user: CurrentUser = Depends(current_user), tx=Depends(user_tx)):
    return await run_in_threadpool(items_service.get_item, user, tx, item_id)


@router.post("/items", response_model=Item, status_code=201, tags=["master-data"])
async def create_item(
    body: ItemCreate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    return await run_in_threadpool(
        items_service.create_item, user, tx, body.model_dump(), request.state.request_id
    )


@router.patch("/items/{item_id}", response_model=Item, tags=["master-data"])
async def update_item(
    item_id: int,
    body: ItemUpdate,
    request: Request,
    user: CurrentUser = Depends(current_user),
    tx=Depends(user_tx),
):
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    changes = {k: v for k, v in changes.items() if v is not None}
    return await run_in_threadpool(
        items_service.update_item,
        user,
        tx,
        item_id,
        changes,
        body.reason,
        request.state.request_id,
    )
