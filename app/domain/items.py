"""Item master rules (FR-010, FR-014, FR-015). Each change and its audit entry commit together."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import psycopg.errors
from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.errors import AppError
from app.repositories import audit
from app.repositories import items as repo

TxFactory = Callable[[], AbstractContextManager[Cursor]]

_AUDITED_FIELDS = ("name", "base_unit", "formulary_status", "controlled_class", "active")


def _snapshot(item: dict) -> dict[str, Any]:
    return {k: item[k] for k in _AUDITED_FIELDS}


def list_items(
    user: CurrentUser, tx: TxFactory, q: str | None = None, active: bool | None = None
) -> list[dict]:
    user.require("items.read")
    with tx() as cur:
        return repo.list_items(cur, user.hospital_id, q, active)


def get_item(user: CurrentUser, tx: TxFactory, item_id: int) -> dict:
    user.require("items.read")
    with tx() as cur:
        item = repo.get_item(cur, user.hospital_id, item_id)
    if item is None:
        raise AppError(404, "NOT_FOUND", "Item not found.")
    return item


def create_item(user: CurrentUser, tx: TxFactory, data: dict, request_id: str) -> dict:
    user.require("items.write")
    try:
        with tx() as cur:
            item = repo.insert_item(cur, user.hospital_id, data)
            audit.append(
                cur,
                hospital_id=user.hospital_id,
                action="create",
                entity_type="item",
                entity_id=str(item["id"]),
                after=_snapshot(item),
                request_id=request_id,
            )
    except psycopg.errors.UniqueViolation as exc:
        raise AppError(409, "CONFLICT", "An item with this name already exists.") from exc
    return item


def update_item(
    user: CurrentUser,
    tx: TxFactory,
    item_id: int,
    changes: dict,
    reason: str | None,
    request_id: str,
) -> dict:
    user.require("items.write")
    if not changes:
        raise AppError(422, "VALIDATION_FAILED", "Nothing to change.")
    try:
        with tx() as cur:
            before = repo.get_item(cur, user.hospital_id, item_id, for_update=True)
            if before is None:
                raise AppError(404, "NOT_FOUND", "Item not found.")
            after = repo.update_item(cur, user.hospital_id, item_id, changes)
            action = "deactivate" if changes.get("active") is False else "update"
            audit.append(
                cur,
                hospital_id=user.hospital_id,
                action=action,
                entity_type="item",
                entity_id=str(item_id),
                before=_snapshot(before),
                after=_snapshot(after),
                reason=reason,
                request_id=request_id,
            )
    except psycopg.errors.UniqueViolation as exc:
        raise AppError(409, "CONFLICT", "An item with this name already exists.") from exc
    return after
