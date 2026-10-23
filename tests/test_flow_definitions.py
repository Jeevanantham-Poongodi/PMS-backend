"""The flow definitions match the spec's Branch Inventory (SC-001): every arrow exists with the
diagram's outcome, every node is reachable, only end nodes have no way out."""

import re
from pathlib import Path

import pytest

from app.flows.definitions import FLOWS, INTERNAL_STARTS, outgoing

SPEC = Path(__file__).resolve().parents[2] / "specs" / "002-flow-pages" / "speck.md"
ROW = re.compile(r'^\| `F(\d):"(.+?)"` \| (.+?) \| `F\d:"(.+?)"`')


def branch_inventory() -> list[tuple[int, str, str | None, str]]:
    rows = []
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            flow, source, outcome, target = match.groups()
            rows.append((int(flow), source, None if outcome == "—" else outcome, target))
    return rows


def label_index(flow: int) -> dict[str, str]:
    return {n.label: n.key for n in FLOWS[flow].nodes}


@pytest.mark.skipif(not SPEC.exists(), reason="spec not in this checkout")
def test_every_branch_inventory_row_is_an_edge():
    rows = branch_inventory()
    assert len(rows) > 90
    for flow, source, outcome, target in rows:
        keys = label_index(flow)
        assert source in keys, f"F{flow} node missing: {source}"
        assert target in keys, f"F{flow} node missing: {target}"
        edges = [e for e in outgoing(flow, keys[source]) if e.target == keys[target]]
        assert edges, f"F{flow}: no arrow {source} -> {target}"
        # Outcome labels follow the diagram; "Stock received (via Flow 1)" is stored as
        # "Stock received", as in the recorded history.
        expected = outcome.replace(" (via Flow 1)", "") if outcome else None
        assert any(e.outcome == expected for e in edges), f"F{flow}: {source} {outcome}"


@pytest.mark.skipif(not SPEC.exists(), reason="spec not in this checkout")
def test_no_edge_beyond_the_branch_inventory():
    assert sum(len(f.edges) for f in FLOWS.values()) == len(branch_inventory())


@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_every_node_is_reachable_from_a_start(flow):
    definition = FLOWS[flow]
    seen = set(definition.starts) | set(INTERNAL_STARTS.get(flow, ()))
    frontier = list(seen)
    while frontier:
        node = frontier.pop()
        for edge in outgoing(flow, node):
            if edge.target not in seen:
                seen.add(edge.target)
                frontier.append(edge.target)
    assert seen == {n.key for n in definition.nodes}


@pytest.mark.parametrize("flow", sorted(FLOWS))
def test_only_end_nodes_have_no_outgoing_arrow(flow):
    for node in FLOWS[flow].nodes:
        assert (node.kind == "end") == (not outgoing(flow, node.key)), node.key


def test_edge_ids_are_unique():
    ids = [e.id for f in FLOWS.values() for e in f.edges]
    assert len(ids) == len(set(ids))


def test_approval_edges_are_exactly_fr_004():
    approval_nodes = {e.source for f in FLOWS.values() for e in f.edges if e.approval}
    assert approval_nodes == {
        "f1_committee", "f1_hod", "f1_finance", "f1_retro", "f2_review", "f4_writeoff"
    }
    for flow in FLOWS.values():
        for edge in flow.edges:
            if edge.source in approval_nodes:
                assert edge.approval, edge.id


def test_reason_edges_lead_into_exactly_fr_005_nodes():
    reason_targets = {e.target for f in FLOWS.values() for e in f.edges if e.reason}
    assert reason_targets == {"f2_return_ward", "f1_reject", "f3_query", "f4_investigate"}
