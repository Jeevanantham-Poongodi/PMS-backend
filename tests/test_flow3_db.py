"""Flow 3 — Patient dispensing: every branch (US3, FR-022, FR-023, SC-001)."""

import pytest

from app.core.errors import AppError


def order(env, item="para", qty=4, order_type="OP"):
    case = env.create(
        3,
        "f3_order",
        {"order_type": order_type, "patient_ref": "OP-1001", "prescriber": "Dr. Rao"},
        [(item, qty)],
    )
    env.act(case)  # → Pharmacist verification
    return case


def test_query_modified_and_cancelled(flow_env):
    env = flow_env
    case = order(env)
    env.act(case, "Rejected or unclear", note="Dose unclear")
    env.act(case)
    env.act(case, "Order modified", order_lines={str(env.line_ids(case)[0]): 2})
    assert env.node(case) == "f3_verify"
    assert env.lines(case)[0]["qty_requested"] == 2
    env.act(case, "Rejected or unclear", note="Still unclear")
    env.act(case)
    env.act(case, "Order cancelled")
    assert env.case(case)["closed"]


def test_op_non_controlled_full_path(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "D1", 200, 10)
    case = order(env)
    env.act(case, "Approved")
    assert env.outcomes(case) == ["Yes"]
    env.act(case, "Yes")
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    env.act(case)  # dispense → IP or OP?
    assert env.balance(batch, "pharmacy") == 6
    assert env.outcomes(case) == ["OP"]
    env.act(case, "OP")
    env.act(case)
    env.act(case)
    env.act(case, "No")
    assert env.case(case)["closed"]


def test_ip_path_and_return_accepted(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "D2", 200, 10)
    case = order(env, order_type="IP")
    env.act(case, "Approved")
    env.act(case, "Yes")
    env.act(case, "No")
    env.act(case)
    assert env.outcomes(case) == ["IP"]
    env.act(case, "IP")
    env.act(case)  # → Nurse administers
    env.act(case)  # → Unused medicine returned?
    env.act(case, "Yes", return_qty={str(env.line_ids(case)[0]): 1})
    env.act(case, "Accepted")
    assert env.balance(batch, "pharmacy") == 7
    env.act(case)
    assert env.case(case)["closed"]


def test_return_rejected_goes_to_quarantine_and_flow4(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "D3", 200, 10)
    case = order(env)
    env.act(case, "Approved")
    env.act(case, "Yes")
    env.act(case, "No")
    env.act(case)
    env.act(case, "OP")
    env.act(case)
    env.act(case)
    env.act(case, "Yes", return_qty={str(env.line_ids(case)[0]): 2})
    env.act(case, "Rejected")
    assert env.balance(batch, "quarantine_expiry") == 2
    assert env.case(env.case(case)["child_case_ids"][0])["node"] == "f4_expired"


def test_narcotic_needs_ndps_entry(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "morph", "N1", 200, 10)
    case = order(env, "morph", 2)
    env.act(case, "Approved")
    env.act(case, "Yes")
    assert env.outcomes(case) == ["Yes"]
    env.act(case, "Yes")
    env.act(case)  # NDPS entry → Dispense
    register = env.cur.execute(
        "select count(*) as n from public.ndps_register where case_id = %s", (case,)
    ).fetchone()["n"]
    assert register == 1
    env.act(case)
    assert env.balance(batch, "pharmacy") == 8


def test_ndps_needs_two_users_outside_demo_mode(flow_env, strict_mode):
    env = flow_env
    env.stock_in("pharmacy", "morph", "N2", 200, 10)
    case = order(env, "morph", 2)
    env.act(case, "Approved")
    env.act(case, "Yes")
    env.act(case, "Yes")
    with pytest.raises(AppError) as error:
        env.act(case)
    assert error.value.code == "SAME_SIGNATORY"


def test_not_in_stock_substitute(flow_env):
    env = flow_env
    env.stock_in("pharmacy", "amox", "SUB", 200, 10)
    case = order(env, "para", 3)
    env.act(case, "Approved")
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    env.act(case, substitute={"choice": "substitute", "item_id": env.items["amox"]})
    assert env.node(case) == "f3_controlled"
    assert env.lines(case)[0]["item_name"].startswith("Amoxicillin")


def test_not_in_stock_emergency_purchase_opens_flow1(flow_env):
    env = flow_env
    case = order(env, "para", 3)
    env.act(case, "Approved")
    env.act(case, "No")
    env.act(case, substitute={"choice": "emergency_purchase"})
    child = env.case(env.case(case)["child_case_ids"][0])
    assert child["flow"] == 1 and child["node"] == "f1_emergency"
