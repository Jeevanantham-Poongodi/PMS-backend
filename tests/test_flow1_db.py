"""Flow 1 — Procurement: every branch (US1, FR-020, SC-001)."""

from datetime import date, timedelta

import pytest

from app.core.errors import AppError

EXPIRY = (date.today() + timedelta(days=400)).isoformat()


def requisition(env, item="para", qty=10):
    case = env.create(1, "f1_need", {"trigger": "reorder_level"}, [(item, qty)])
    env.act(case)  # Store raises purchase requisition
    env.act(case)  # → Formulary item?
    return case


def to_goods(env, case):
    env.act(case, "Yes")
    env.act(case, "Approved")
    env.act(case, "Yes", vendor_id=env.vendor_id)  # Rate contract exists → Create PO
    env.act(case)  # → Finance approves PO?
    env.act(case, "Approved")
    env.act(case)  # → Vendor response
    env.act(case, "Confirmed")
    env.act(case)  # → GRN check


def batches(env, case, batch_no="GRN1"):
    return {str(i): {"batch_no": batch_no, "expiry_date": EXPIRY} for i in env.line_ids(case)}


def received_batch(env, case):
    return env.lines(case)[0]["batch_id"]


def test_formulary_path_to_invoice_matched(flow_env):
    env = flow_env
    case = requisition(env)
    assert env.outcomes(case) == ["Yes"]
    to_goods(env, case)
    env.act(case, "All OK", batches=batches(env, case))
    batch = received_batch(env, case)
    assert env.balance(batch, "quarantine_qc") == 10
    env.act(case)  # → QC passed?
    env.act(case, "Pass")
    assert env.balance(batch, "quarantine_qc") == 0
    assert env.balance(batch, "central_store") == 10
    env.act(case)
    detail = env.case(case)
    assert detail["node"] == "f1_invoice" and detail["closed"]


def test_non_formulary_goes_to_committee_and_can_be_rejected(flow_env):
    env = flow_env
    case = requisition(env, "nonf")
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    env.act(case)  # → Committee approves?
    env.act(case, "Rejected", note="Use the formulary syrup")
    assert env.node(case) == "f1_reject"
    env.act(case)
    assert env.node(case) == "f1_closed"


def test_committee_approval_leads_to_hod(flow_env):
    env = flow_env
    case = requisition(env, "nonf")
    env.act(case, "No")
    env.act(case)
    env.act(case, "Approved")
    assert env.node(case) == "f1_hod"


def test_hod_reject_resubmit_and_drop(flow_env):
    env = flow_env
    case = requisition(env)
    env.act(case, "Yes")
    env.act(case, "Rejected")
    env.act(case, "Resubmit")
    assert env.node(case) == "f1_hod"
    env.act(case, "Rejected")
    env.act(case, "Drop")
    assert env.case(case)["closed"]


def test_partial_hod_approval_quantity(flow_env):
    env = flow_env
    case = requisition(env)
    env.act(case, "Yes")
    line = env.line_ids(case)[0]
    env.act(case, "Approved", approved_qty={str(line): 4})
    assert env.lines(case)[0]["qty_approved"] == 4
    with pytest.raises(AppError):
        case2 = requisition(env)
        env.act(case2, "Yes")
        env.act(case2, "Approved", approved_qty={str(env.line_ids(case2)[0]): 99})


def test_rfq_path_and_finance_revise(flow_env):
    env = flow_env
    case = requisition(env)
    env.act(case, "Yes")
    env.act(case, "Approved")
    env.act(case, "No")
    env.act(case)  # RFQ → Comparative statement
    env.act(case, vendor_id=env.vendor_id)
    env.act(case)
    env.act(case, "Rejected")
    env.act(case, "Resubmit")
    assert env.node(case) == "f1_finance"


def test_vendor_partial_and_declined(flow_env):
    env = flow_env
    case = requisition(env)
    env.act(case, "Yes")
    env.act(case, "Approved")
    env.act(case, "Yes", vendor_id=env.vendor_id)
    env.act(case)
    env.act(case, "Approved")
    env.act(case)
    env.act(case, "Declined")
    env.act(case)  # Pick alternate vendor → Comparative statement
    assert env.node(case) == "f1_comparative"
    env.act(case, vendor_id=env.vendor_id)
    env.act(case)
    env.act(case, "Approved")
    env.act(case)
    env.act(case, "Partial")
    env.act(case, "Balance arrives later")
    assert env.node(case) == "f1_goods_arrive"


def test_grn_short_posts_nothing(flow_env):
    env = flow_env
    case = requisition(env)
    to_goods(env, case)
    env.act(case, "Short")
    assert env.node(case) == "f1_backorder"
    assert env.lines(case)[0]["batch_id"] is None


def test_grn_wrong_or_damaged_then_reorder(flow_env):
    env = flow_env
    case = requisition(env)
    to_goods(env, case)
    env.act(case, "Wrong or damaged")
    env.act(case, "Reorder")
    assert env.node(case) == "f1_create_po"


def test_grn_requires_batch_and_expiry(flow_env):
    env = flow_env
    case = requisition(env)
    to_goods(env, case)
    with pytest.raises(AppError) as error:
        env.act(case, "All OK")
    assert error.value.code == "VALIDATION_FAILED"


def test_qc_fail_returns_stock_to_vendor(flow_env):
    env = flow_env
    case = requisition(env)
    to_goods(env, case)
    env.act(case, "All OK", batches=batches(env, case, "BADQC"))
    batch = received_batch(env, case)
    env.act(case)
    env.act(case, "Fail")
    assert env.node(case) == "f1_return_vendor"
    assert env.balance(batch, "quarantine_qc") == 0


def test_emergency_purchase_approved_and_rejected(flow_env):
    env = flow_env
    case = env.create(1, "f1_emergency", {"vendor_id": env.vendor_id}, [("amox", 6)])
    env.act(case)
    env.act(case, "Approved", batches=batches(env, case, "EMG1"))
    assert env.balance(received_batch(env, case), "central_store") == 6
    assert env.node(case) == "f1_stock_central"

    rejected = env.create(1, "f1_emergency", {}, [("amox", 2)])
    env.act(rejected)
    env.act(rejected, "Rejected")
    assert env.node(rejected) == "f1_audit" and env.case(rejected)["closed"]
