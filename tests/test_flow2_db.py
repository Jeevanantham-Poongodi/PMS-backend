"""Flow 2 — Ward indent: every branch (US2, FR-021, FR-021a, SC-001)."""

import pytest

from app.core.errors import AppError


def indent(env, qty=8, item="para"):
    case = env.create(2, "f2_need", {"ward_location_id": env.loc["ward"]}, [(item, qty)])
    env.act(case)  # Ward raises indent
    env.act(case)  # → Pharmacist reviews indent
    return case


def test_rejected_then_resubmitted(flow_env):
    env = flow_env
    case = indent(env)
    env.act(case, "Rejected", note="Quantity too high")
    env.act(case, "Revise and resubmit")
    assert env.node(case) == "f2_raise"


def test_fefo_issue_split_and_full_cycle(flow_env):
    env = flow_env
    late = env.stock_in("pharmacy", "para", "LATE", 300, 10)
    early = env.stock_in("pharmacy", "para", "EARLY", 100, 5)
    case = indent(env, 8)
    env.act(case, "Approved (full or partial)")
    assert env.outcomes(case) == ["Yes"]
    env.act(case, "Yes")
    env.act(case)  # issue → Ward receipt matches issue?
    assert env.balance(early, "pharmacy") == 0
    assert env.balance(late, "pharmacy") == 7
    assert env.balance(early, "ward") == 5 and env.balance(late, "ward") == 3
    env.act(case, "Yes")
    env.act(case)  # Ward stock updated → Unused?
    env.act(case, "No")
    assert env.case(case)["closed"]


def test_partial_approval_issues_approved_quantity(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "P1", 200, 20)
    case = indent(env, 10)
    env.act(case, "Approved (full or partial)", approved_qty={str(env.line_ids(case)[0]): 4})
    env.act(case, "Yes")
    env.act(case)
    assert env.balance(batch, "ward") == 4


def test_shortfall_raises_linked_requisition_and_refills(flow_env):
    env = flow_env
    env.stock_in("pharmacy", "para", "PH", 200, 2)
    central = env.stock_in("central_store", "para", "CS", 250, 20)
    case = indent(env, 8)
    env.act(case, "Approved (full or partial)")
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    children = env.case(case)["child_case_ids"]
    assert len(children) == 1
    child = env.case(children[0])
    assert child["flow"] == 1 and child["lines"][0]["qty_requested"] == 6
    env.act(case, "Stock received")
    assert env.balance(central, "pharmacy") == 6
    env.act(case)
    assert env.node(case) == "f2_receipt"


def test_stock_received_refused_while_central_is_short(flow_env):
    env = flow_env
    case = indent(env, 8)
    env.act(case, "Approved (full or partial)")
    env.act(case, "No")
    with pytest.raises(AppError) as error:
        env.act(case, "Stock received")
    assert error.value.code == "INSUFFICIENT_STOCK"


def issued_case(env):
    batch = env.stock_in("pharmacy", "para", "RET", 200, 10)
    case = indent(env, 6)
    env.act(case, "Approved (full or partial)")
    env.act(case, "Yes")
    env.act(case)
    return case, batch


def test_discrepancy_path(flow_env):
    case, _ = issued_case(flow_env)
    flow_env.act(case, "No", note="Received 5, issued 6")
    flow_env.act(case)
    assert flow_env.node(case) == "f2_ward_stock"


def test_return_accepted_restocks_pharmacy(flow_env):
    env = flow_env
    case, batch = issued_case(env)
    env.act(case, "Yes")
    env.act(case)
    env.act(case, "Yes", return_qty={str(env.line_ids(case)[0]): 2})
    env.act(case)
    env.act(case, "Accepted")
    assert env.balance(batch, "ward") == 4 and env.balance(batch, "pharmacy") == 6
    env.act(case)
    assert env.case(case)["closed"]


def test_return_rejected_quarantines_and_opens_flow4_case(flow_env):
    env = flow_env
    case, batch = issued_case(env)
    env.act(case, "Yes")
    env.act(case)
    env.act(case, "Yes", return_qty={str(env.line_ids(case)[0]): 2})
    env.act(case)
    env.act(case, "Rejected")
    assert env.balance(batch, "quarantine_expiry") == 2
    child = env.case(env.case(case)["child_case_ids"][0])
    assert child["flow"] == 4 and child["node"] == "f4_expired"
    env.act(case)
    assert env.node(case) == "f2_closed"


def test_blank_quantity_fields_are_ignored_and_issue_records_ward(flow_env):
    """Rehearsal defects D2 and D4 (feature 004): a blank quantity in a dialog is "not entered",
    and issued lines record the ward they went to."""
    env = flow_env
    env.stock_in("pharmacy", "para", "BL1", 200, 10)
    env.stock_in("pharmacy", "amox", "BL2", 200, 10)
    case = env.create(
        2, "f2_need", {"ward_location_id": env.loc["ward"]}, [("para", 4), ("amox", 4)]
    )
    env.act(case)
    env.act(case)
    first, second = env.line_ids(case)
    env.act(case, "Approved (full or partial)", approved_qty={str(first): "", str(second): 3})
    assert [line["qty_approved"] for line in env.lines(case)] == [4, 3]
    env.act(case, "Yes")
    env.act(case)
    assert {line["location_name"] for line in env.lines(case)} == {"Ward A"}
    env.act(case, "Yes")
    env.act(case)
    env.act(case, "Yes", return_qty={str(first): "", str(second): 1})
    assert env.case(case)["data"]["returns"] == {str(second): "1"}
