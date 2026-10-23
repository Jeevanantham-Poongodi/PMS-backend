"""FEFO split (D-005 order is the candidate query's job, tested in test_flows_db.py; D-013 split
and shortfall are tested here without a database)."""

from decimal import Decimal

from app.domain.stock import Pick, split_fefo


def rows(*quantities):
    return [{"batch_id": i + 1, "qty": Decimal(q)} for i, q in enumerate(quantities)]


def test_takes_the_first_batch_when_it_covers():
    picks, short = split_fefo(rows(50, 30), Decimal(20))
    assert picks == [Pick(1, Decimal(20))] and short == 0


def test_splits_across_batches_in_order():
    picks, short = split_fefo(rows(10, 30, 40), Decimal(35))
    assert picks == [Pick(1, Decimal(10)), Pick(2, Decimal(25))] and short == 0


def test_reports_the_shortfall():
    picks, short = split_fefo(rows(10, 5), Decimal(25))
    assert picks == [Pick(1, Decimal(10)), Pick(2, Decimal(5))] and short == Decimal(10)


def test_nothing_to_allocate():
    assert split_fefo([], Decimal(5)) == ([], Decimal(5))
