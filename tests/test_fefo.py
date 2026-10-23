"""FEFO allocation (constitution IV; D-005, D-012, D-013; SC-004)."""

from datetime import date
from decimal import Decimal

from app.domain.stock import allocate_fefo, split_fefo
from app.repositories import masters


def rows(*qtys):
    return [
        {"batch_id": i, "batch_no": f"B{i}", "expiry_date": date(2030, 1, i), "qty": Decimal(q)}
        for i, q in enumerate(qtys, start=1)
    ]


def test_split_takes_batches_in_order_until_covered():
    allocation = split_fefo(rows(5, 10, 10), Decimal(12))
    assert [(p.batch_id, p.qty) for p in allocation.picks] == [(1, 5), (2, 7)]
    assert allocation.covered


def test_split_reports_shortfall():
    allocation = split_fefo(rows(5, 3), Decimal(10))
    assert allocation.shortfall == 2
    assert sum(p.qty for p in allocation.picks) == 8


# --- against the database ---------------------------------------------------


def allocate(env, item, kind, qty):
    return allocate_fefo(env.cur, env.hospital_id, env.items[item], env.loc[kind], Decimal(qty))


def test_earlier_expiry_first(flow_env):
    late = flow_env.stock_in("pharmacy", "para", "LATE", 300, 10)
    early = flow_env.stock_in("pharmacy", "para", "EARLY", 100, 10)
    assert [p.batch_id for p in allocate(flow_env, "para", "pharmacy", 15).picks] == [early, late]


def test_same_expiry_earlier_receipt_first(flow_env):
    first = flow_env.stock_in("pharmacy", "para", "R1", 100, 5)
    second = flow_env.stock_in("pharmacy", "para", "R2", 100, 5)
    flow_env.cur.execute(
        "update public.batches set first_receipt_at = now() - interval '2 days' where id = %s",
        (second,),
    )
    assert [p.batch_id for p in allocate(flow_env, "para", "pharmacy", 10).picks] == [second, first]


def test_same_expiry_and_receipt_lower_batch_id_first(flow_env):
    a = flow_env.stock_in("pharmacy", "para", "S1", 100, 5)
    b = flow_env.stock_in("pharmacy", "para", "S2", 100, 5)
    flow_env.cur.execute(
        "update public.batches set first_receipt_at = '2026-01-01' where id = any(%s)", ([a, b],)
    )
    picks = allocate(flow_env, "para", "pharmacy", 10).picks
    assert [p.batch_id for p in picks] == sorted([a, b])
    assert [p.batch_id for p in allocate(flow_env, "para", "pharmacy", 10).picks] == sorted([a, b])


def test_batch_expiring_today_is_not_allocated(flow_env):
    flow_env.stock_in("pharmacy", "para", "TODAY", 0, 10)
    assert allocate(flow_env, "para", "pharmacy", 1).shortfall == 1


def test_expiry_boundary_is_configuration(flow_env):
    batch = flow_env.stock_in("pharmacy", "para", "TODAY", 0, 10)
    masters.put_setting(
        flow_env.cur, flow_env.hospital_id, "allocation.block_on_expiry_date", False
    )
    assert [p.batch_id for p in allocate(flow_env, "para", "pharmacy", 1).picks] == [batch]


def test_blocked_batch_is_not_allocated(flow_env):
    batch = flow_env.stock_in("pharmacy", "para", "BLK", 100, 10)
    masters.block_batch(flow_env.cur, flow_env.hospital_id, batch)
    assert allocate(flow_env, "para", "pharmacy", 1).shortfall == 1


def test_quarantine_stock_is_never_allocated(flow_env):
    flow_env.stock_in("quarantine_qc", "para", "QC", 100, 10)
    assert allocate(flow_env, "para", "quarantine_qc", 1).shortfall == 1
