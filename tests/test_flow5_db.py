"""Flow 5 — Recall: every branch (US5, FR-025, SC-001)."""

from decimal import Decimal

from app.domain import stock
from app.domain.stock import allocate_fefo


def recall(env, batch):
    case = env.create(
        5,
        "f5_notice",
        {"batch_id": batch, "source": "regulator"},
        [("para", 1, {"batch_id": batch})],
    )
    env.act(case)  # → Find affected batches
    return case


def test_recall_quarantines_everywhere_and_traces_patients(flow_env):
    env = flow_env
    batch = env.stock_in("central_store", "para", "RC1", 300, 20)
    stock.post_document(
        env.cur,
        env.user,
        "transfer",
        [
            {
                "item_id": env.items["para"],
                "batch_id": batch,
                "location_id": env.loc["central_store"],
                "qty": Decimal(-12),
            },
            {
                "item_id": env.items["para"],
                "batch_id": batch,
                "location_id": env.loc["pharmacy"],
                "qty": Decimal(8),
            },
            {
                "item_id": env.items["para"],
                "batch_id": batch,
                "location_id": env.loc["ward"],
                "qty": Decimal(4),
            },
        ],
        request_id=None,
    )
    stock.post_document(
        env.cur,
        env.user,
        "dispense",
        [
            {
                "item_id": env.items["para"],
                "batch_id": batch,
                "location_id": env.loc["pharmacy"],
                "qty": Decimal(-3),
            }
        ],
        request_id=None,
        patient_ref="IP-2001",
        prescriber="Dr. Iyer",
    )
    case = recall(env, batch)
    holdings = env.case(case)["data"]["holdings"]
    assert len(holdings) == 3
    env.act(case)  # → Block and quarantine batches (stock moves here)
    env.act(case)  # → Already dispensed to patients?
    for kind in ("central_store", "pharmacy", "ward"):
        assert env.balance(batch, kind) == 0
    assert env.balance(batch, "quarantine_recall") == 17
    assert (
        allocate_fefo(
            env.cur, env.hospital_id, env.items["para"], env.loc["pharmacy"], Decimal(1)
        ).shortfall
        == 1
    )
    assert env.outcomes(case) == ["Yes"]
    env.act(case, "Yes")
    env.act(case)  # → Return to vendor
    env.act(case)
    assert env.balance(batch, "quarantine_recall") == 0
    assert env.case(case)["closed"]


def test_recall_not_dispensed(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "RC2", 300, 5)
    case = recall(env, batch)
    env.act(case)
    env.act(case)
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    env.act(case)
    assert env.case(case)["closed"]
