"""Audit writes go through app.audit_append, which chains the hash and records the signed-in
user as the actor (constitution VII). Runs in the same transaction as the change it records."""

import json
from datetime import date, datetime
from typing import Any

from psycopg import Cursor


def _json(value: Any) -> str | None:
    if value is None:
        return None

    def default(o: Any) -> str:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(type(o).__name__)

    return json.dumps(value, default=default)


def append(
    cur: Cursor,
    *,
    hospital_id: int,
    action: str,
    entity_type: str,
    entity_id: str,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    request_id: str | None = None,
) -> int:
    cur.execute(
        "select app.audit_append(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) as id",
        (
            hospital_id,
            action,
            entity_type,
            entity_id,
            _json(before),
            _json(after),
            reason,
            request_id,
        ),
    )
    return cur.fetchone()["id"]
