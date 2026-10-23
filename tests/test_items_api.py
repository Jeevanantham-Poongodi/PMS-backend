"""API tests without a database: the transaction and repositories are replaced by fakes, so
these check routing, validation, permissions, the error envelope and audit pairing."""

from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg.errors
import pytest
from fastapi.testclient import TestClient

from app.api.v1 import routes
from app.core import auth
from app.core.auth import CurrentUser
from app.core.config import Settings, get_settings
from app.domain import items as items_service
from app.main import app

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def make_user(permissions=("items.read", "items.write"), privileged=False, aal="aal1"):
    return CurrentUser(
        user_id="11111111-1111-1111-1111-111111111111",
        hospital_id=7,
        display_name="Asha",
        roles=frozenset({"store"}),
        permissions=frozenset(permissions),
        privileged=privileged,
        mfa_satisfied=aal == "aal2",
        claims={"sub": "11111111-1111-1111-1111-111111111111"},
    )


class FakeTx:
    """Counts transactions; the cursor object identifies which transaction a call ran in."""

    def __init__(self):
        self.opened = 0

    def __call__(self):
        @contextmanager
        def _tx():
            self.opened += 1
            yield f"cursor-{self.opened}"

        return _tx()


@pytest.fixture
def fake_tx():
    return FakeTx()


@pytest.fixture
def client(fake_tx):
    app.dependency_overrides[routes.user_tx] = lambda: fake_tx
    # demo_mode pinned off so a local .env with DEMO_MODE=true doesn't skip the MFA checks.
    app.dependency_overrides[get_settings] = lambda: Settings(database_url=None, demo_mode=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def as_user(user):
    app.dependency_overrides[auth.current_user] = lambda: user


def item_row(**overrides):
    row = {
        "id": 1,
        "name": "Paracetamol 500 mg",
        "base_unit": "tablet",
        "formulary_status": "formulary",
        "controlled_class": "none",
        "active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def test_health_reports_missing_database(client, monkeypatch):
    monkeypatch.setattr(routes, "get_settings", lambda: Settings(database_url=None))
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "database": "not_configured"}
    assert response.headers["X-Request-ID"]


def test_request_without_token_gets_envelope(client):
    response = client.get("/api/v1/items", headers={"X-Request-ID": "req-123"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["request_id"] == "req-123"


def test_create_item_writes_item_and_audit_in_one_transaction(client, fake_tx, monkeypatch):
    as_user(make_user())
    calls = []
    monkeypatch.setattr(
        items_service.repo,
        "insert_item",
        lambda cur, hid, data: calls.append(("insert", cur, hid)) or item_row(),
    )
    monkeypatch.setattr(
        items_service.audit, "append", lambda cur, **kw: calls.append(("audit", cur, kw)) or 1
    )

    response = client.post(
        "/api/v1/items",
        json={
            "name": "  Paracetamol 500 mg ",
            "base_unit": "tablet",
            "formulary_status": "formulary",
            "controlled_class": "none",
        },
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Paracetamol 500 mg"
    assert fake_tx.opened == 1
    (_, insert_cur, hospital_id), (_, audit_cur, audit_kw) = calls
    assert insert_cur == audit_cur == "cursor-1"  # same transaction
    assert hospital_id == 7  # hospital comes from membership
    assert audit_kw["action"] == "create" and audit_kw["after"]["formulary_status"] == "formulary"
    assert audit_kw["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "body, field",
    [
        (
            {
                "name": "X",
                "base_unit": "tablet",
                "formulary_status": "maybe",
                "controlled_class": "none",
            },
            "formulary_status",
        ),
        (
            {
                "name": "X",
                "base_unit": "tablet",
                "formulary_status": "formulary",
                "controlled_class": "opioid",
            },
            "controlled_class",
        ),
        (
            {
                "name": "   ",
                "base_unit": "tablet",
                "formulary_status": "formulary",
                "controlled_class": "none",
            },
            "name",
        ),
        ({"name": "X", "formulary_status": "formulary", "controlled_class": "none"}, "base_unit"),
    ],
)
def test_create_item_validation(client, body, field):
    as_user(make_user())
    response = client.post("/api/v1/items", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert any(d["field"] == field for d in error["details"])


def test_client_cannot_choose_hospital(client):
    as_user(make_user())
    response = client.post(
        "/api/v1/items",
        json={
            "name": "X",
            "base_unit": "tablet",
            "formulary_status": "formulary",
            "controlled_class": "none",
            "hospital_id": 99,
        },
    )
    assert response.status_code == 422


def test_create_without_permission_is_refused_before_database(client, fake_tx):
    as_user(make_user(permissions=("items.read",)))
    response = client.post(
        "/api/v1/items",
        json={
            "name": "X",
            "base_unit": "tablet",
            "formulary_status": "formulary",
            "controlled_class": "none",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert fake_tx.opened == 0


def test_list_requires_read_permission(client):
    as_user(make_user(permissions=()))
    assert client.get("/api/v1/items").status_code == 403


def test_duplicate_name_is_conflict(client, monkeypatch):
    as_user(make_user())

    def duplicate(*_args):
        raise psycopg.errors.UniqueViolation("duplicate")

    monkeypatch.setattr(items_service.repo, "insert_item", duplicate)
    response = client.post(
        "/api/v1/items",
        json={
            "name": "X",
            "base_unit": "tablet",
            "formulary_status": "formulary",
            "controlled_class": "none",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_deactivate_is_audited_with_before_and_after(client, monkeypatch):
    as_user(make_user())
    audits = []
    monkeypatch.setattr(
        items_service.repo, "get_item", lambda cur, hid, iid, for_update=False: item_row()
    )
    monkeypatch.setattr(
        items_service.repo, "update_item", lambda cur, hid, iid, changes: item_row(**changes)
    )
    monkeypatch.setattr(items_service.audit, "append", lambda cur, **kw: audits.append(kw) or 1)

    response = client.patch("/api/v1/items/1", json={"active": False, "reason": "Discontinued"})

    assert response.status_code == 200 and response.json()["active"] is False
    assert audits[0]["action"] == "deactivate"
    assert audits[0]["before"]["active"] is True and audits[0]["after"]["active"] is False
    assert audits[0]["reason"] == "Discontinued"


def test_there_is_no_delete_route(client):
    as_user(make_user())
    assert client.delete("/api/v1/items/1").status_code == 405


def test_privileged_role_without_mfa_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "verify_token", lambda token, settings: {"sub": "u", "aal": "aal1"})
    monkeypatch.setattr(auth, "load_user", lambda claims: make_user(privileged=True, aal="aal1"))
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = lambda: Settings(database_url=None, demo_mode=False)
    response = TestClient(app).get("/api/v1/me", headers={"Authorization": "Bearer t"})
    app.dependency_overrides.clear()
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MFA_REQUIRED"


def test_privileged_role_with_mfa_is_allowed(monkeypatch):
    monkeypatch.setattr(auth, "verify_token", lambda token, settings: {"sub": "u", "aal": "aal2"})
    monkeypatch.setattr(auth, "load_user", lambda claims: make_user(privileged=True, aal="aal2"))
    app.dependency_overrides.clear()
    response = TestClient(app).get("/api/v1/me", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    assert response.json()["hospital_id"] == 7
