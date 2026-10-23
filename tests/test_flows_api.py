"""Flow API contract without a database (as test_items_api.py): routing, response shapes,
validation and the error envelope. The domain layer is replaced where it would query."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import routes
from app.core import auth
from app.core.auth import CurrentUser
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.domain import flows as flows_service
from app.main import app

NOW = datetime(2026, 10, 22, tzinfo=UTC)
ALL = ("flows.read", "flows.write", "flows.approve", "stock.read", "masters.read")


def make_user(permissions=ALL):
    return CurrentUser(
        user_id="11111111-1111-1111-1111-111111111111",
        hospital_id=7,
        display_name="Asha",
        roles=frozenset({"admin"}),
        permissions=frozenset(permissions),
        privileged=False,
        mfa_satisfied=False,
        claims={"sub": "11111111-1111-1111-1111-111111111111"},
    )


@pytest.fixture
def client():
    app.dependency_overrides[routes.user_tx] = lambda: None
    app.dependency_overrides[get_settings] = lambda: Settings(database_url=None, demo_mode=False)
    app.dependency_overrides[auth.current_user] = lambda: make_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


def detail(**overrides):
    body = {
        "id": 5,
        "flow": 2,
        "node": "f2_review",
        "node_label": "Pharmacist reviews indent",
        "title": "Indent from ICU",
        "closed": False,
        "parent_case_id": None,
        "created_at": NOW,
        "updated_at": NOW,
        "data": {"ward_location_id": 3},
        "lines": [],
        "events": [],
        "actions": [flows_service.edge_dict(e) for e in flows_service.outgoing(2, "f2_review")],
        "child_case_ids": [],
    }
    body.update(overrides)
    return body


def test_flows_returns_five_definitions_with_from_and_to(client):
    response = client.get("/api/v1/flows")
    assert response.status_code == 200
    flows = response.json()
    assert [f["flow"] for f in flows] == [1, 2, 3, 4, 5]
    edge = next(e for e in flows[1]["edges"] if e["id"] == "f2_review__rejected")
    assert edge == {
        "id": "f2_review__rejected",
        "from": "f2_review",
        "to": "f2_return_ward",
        "outcome": "Rejected",
        "approval": True,
        "reason": True,
        "inputs": [],
    }
    assert flows[0]["starts"] == ["f1_need", "f1_emergency"]


def test_flows_requires_read_permission(client):
    app.dependency_overrides[auth.current_user] = lambda: make_user(permissions=())
    response = client.get("/api/v1/flows")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_create_case_returns_detail(client, monkeypatch):
    seen = {}

    def create(user, tx, body, request_id):
        seen.update(body)
        return detail()

    monkeypatch.setattr(flows_service, "create_case", create)
    response = client.post(
        "/api/v1/cases",
        json={
            "flow": 2,
            "start": "f2_need",
            "title": "Indent from ICU",
            "data": {"ward_location_id": 3},
            "lines": [{"item_id": 1, "qty": 10}],
        },
    )
    assert response.status_code == 201
    assert response.json()["actions"][0]["from"] == "f2_review"
    assert seen["lines"] == [{"item_id": 1, "qty": 10.0, "batch_id": None, "location_id": None}]


@pytest.mark.parametrize(
    "body",
    [
        {"flow": 6, "start": "x", "title": "T"},
        {"flow": 2, "start": "f2_need", "title": " "},
        {"flow": 2, "start": "f2_need", "title": "T", "lines": [{"item_id": 1, "qty": 0}]},
        {"flow": 2, "start": "f2_need", "title": "T", "hospital_id": 9},
    ],
)
def test_create_case_validation(client, body):
    response = client.post("/api/v1/cases", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "error",
    [
        AppError(409, "INVALID_TRANSITION", "Not available."),
        AppError(409, "CASE_CLOSED", "Closed."),
        AppError(403, "SELF_APPROVAL_FORBIDDEN", "No."),
        AppError(409, "INSUFFICIENT_STOCK", "Short."),
    ],
)
def test_action_errors_use_the_envelope(client, monkeypatch, error):
    def refuse(*_args):
        raise error

    monkeypatch.setattr(flows_service, "take_action", refuse)
    response = client.post(
        "/api/v1/cases/5/actions", json={"edge": "f2_review__rejected"},
        headers={"X-Request-ID": "req-9"},
    )
    assert response.status_code == error.status
    assert response.json()["error"]["code"] == error.code
    assert response.json()["error"]["request_id"] == "req-9"


def test_action_passes_demo_mode_from_settings(client, monkeypatch):
    seen = {}

    def take(user, tx, case_id, body, request_id, demo_mode):
        seen.update(case_id=case_id, body=body, demo_mode=demo_mode)
        return detail(node="f2_return_ward", actions=[])

    monkeypatch.setattr(flows_service, "take_action", take)
    response = client.post(
        "/api/v1/cases/5/actions", json={"edge": "f2_review__rejected", "note": "Wrong ward"}
    )
    assert response.status_code == 200
    assert seen == {
        "case_id": 5,
        "body": {"edge": "f2_review__rejected", "note": "Wrong ward", "input": None},
        "demo_mode": False,
    }


def test_action_rejects_unknown_fields(client):
    response = client.post("/api/v1/cases/5/actions", json={"edge": "x", "node": "f2_closed"})
    assert response.status_code == 422
