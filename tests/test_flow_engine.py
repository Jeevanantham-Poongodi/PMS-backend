"""Case engine rules (FR-002–FR-005; constitution V with the D-018 demo exception)."""

import pytest

from app.core.errors import AppError
from app.domain import flows


def test_outcome_not_offered_is_refused(flow_env):
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    with pytest.raises(AppError) as error:
        flows.move(flow_env.cur, flow_env.user, case, "f2_review__rejected", "x", None, None)
    assert error.value.code == "INVALID_TRANSITION"


def test_closed_case_is_refused(flow_env):
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    flow_env.cur.execute("update public.flow_cases set closed = true where id = %s", (case,))
    with pytest.raises(AppError) as error:
        flow_env.act(case)
    assert error.value.code == "CASE_CLOSED"


def test_reason_is_required(flow_env):
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    flow_env.act(case)
    flow_env.act(case)
    with pytest.raises(AppError) as error:
        flow_env.act(case, "Rejected")
    assert error.value.code == "VALIDATION_FAILED"
    flow_env.act(case, "Rejected", note="Wrong ward")
    assert flow_env.node(case) == "f2_return_ward"


def test_raiser_cannot_approve_outside_demo_mode(flow_env, strict_mode):
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    flow_env.act(case)
    flow_env.act(case)
    with pytest.raises(AppError) as error:
        flow_env.act(case, "Rejected", note="no")
    assert error.value.code == "SELF_APPROVAL_FORBIDDEN"


def test_raiser_may_approve_in_demo_mode(flow_env, monkeypatch):
    monkeypatch.setattr(flows, "get_settings", lambda: type("S", (), {"demo_mode": True})())
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    flow_env.act(case)
    flow_env.act(case)
    flow_env.act(case, "Rejected", note="Demo")
    assert flow_env.node(case) == "f2_return_ward"


def test_every_move_is_in_history(flow_env):
    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    flow_env.act(case)
    events = flow_env.case(case)["events"]
    assert [(e["from_node"], e["to_node"]) for e in events] == [
        (None, "f2_need"),
        ("f2_need", "f2_raise"),
    ]


def test_history_cannot_be_changed(flow_env):
    import psycopg

    case = flow_env.create(2, "f2_need", {"ward_location_id": flow_env.loc["ward"]}, [("para", 5)])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with flow_env.conn.transaction():
            flow_env.cur.execute("delete from public.flow_events where case_id = %s", (case,))
