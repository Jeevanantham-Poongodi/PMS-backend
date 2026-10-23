"""Contract checks for the feature 002 endpoints without a database (contracts/openapi.yaml)."""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import routes
from app.core import auth
from app.core.auth import CurrentUser
from app.core.errors import AppError
from app.domain import flows as flows_service
from app.main import app

ALL = ("flows.read", "flows.write", "flows.approve", "stock.read", "masters.read")


def make_user(permissions=ALL):
    return CurrentUser(
        user_id="11111111-1111-1111-1111-111111111111",
        hospital_id=7,
        display_name="Asha",
        roles=frozenset({"admin"}),
        permissions=frozenset(permissions),
        privileged=False,
        mfa_satisfied=True,
        claims={"sub": "11111111-1111-1111-1111-111111111111"},
    )


@pytest.fixture
def client():
    @contextmanager
    def tx():
        yield "cursor"

    app.dependency_overrides[routes.user_tx] = lambda: tx
    app.dependency_overrides[auth.current_user] = make_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_flow_definitions_shape(client):
    body = client.get("/api/v1/flows").json()
    assert [f["flow"] for f in body] == [1, 2, 3, 4, 5]
    edge = body[0]["edges"][0]
    assert {"id", "from", "to", "outcome", "approval", "reason", "inputs"} <= set(edge)
    assert body[0]["starts"] == ["f1_need", "f1_emergency"]


def test_definitions_need_permission(client):
    app.dependency_overrides[auth.current_user] = lambda: make_user(permissions=())
    response = client.get("/api/v1/flows")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_create_case_validates_body(client):
    response = client.post("/api/v1/cases", json={"flow": 9, "start": "x", "title": "t"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_action_errors_use_the_envelope(client, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AppError(409, "INVALID_TRANSITION", "That outcome is not available.")

    monkeypatch.setattr(flows_service, "take_action", refuse)
    response = client.post("/api/v1/cases/1/actions", json={"edge": "f2_review__rejected"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INVALID_TRANSITION" and error["request_id"]


def test_action_rejects_unknown_fields(client):
    response = client.post("/api/v1/cases/1/actions", json={"edge": "x", "idempotency": "k"})
    assert response.status_code == 422
