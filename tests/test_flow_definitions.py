"""The flow definitions match the spec's Branch Inventory exactly (SC-001, constitution XIV).

The inventory tables in specs/002-flow-pages/spec.md are parsed and compared with
app/flows/definitions.py, so a change to either without the other fails here.
"""

import re
from pathlib import Path

import pytest

from app.flows.definitions import FLOWS

SPEC = Path(__file__).resolve().parents[2] / "specs" / "002-flow-pages" / "spec.md"
ROW = re.compile(r'^\| `F(\d):"(.+?)"` \| (.+?) \| `F\d:"(.+?)"`')

# Starts added by clarification, not by an arrow in the diagram (spec, Clarifications 2026-10-22).
CLARIFIED_STARTS = {(4, "Expired: quarantine")}


def inventory() -> set[tuple[int, str, str | None, str]]:
    rows = set()
    for line in SPEC.read_text().splitlines():
        match = ROW.match(line)
        if match:
            flow, source, outcome, target = match.groups()
            outcome = outcome.strip()
            rows.add((int(flow), source, None if outcome == "—" else outcome, target))
    return rows


def defined() -> set[tuple[int, str, str | None, str]]:
    rows = set()
    for flow in FLOWS.values():
        for edge in flow.edges:
            rows.add(
                (
                    flow.number,
                    flow.node(edge.source).label,
                    edge.outcome,
                    flow.node(edge.target).label,
                )
            )
    return rows


def normalise(rows):
    # The spec annotates one outcome with where the stock comes from; the label is the diagram's.
    return {(f, s, (o or "").replace(" (via Flow 1)", "") or None, t) for f, s, o, t in rows}


def test_spec_inventory_was_parsed():
    assert len(inventory()) > 100


def test_every_inventory_arrow_is_defined_and_nothing_else():
    assert normalise(inventory()) == normalise(defined())


@pytest.mark.parametrize("flow", FLOWS.values(), ids=lambda f: f.title)
def test_every_node_is_reachable_from_a_start(flow):
    reached, frontier = set(flow.starts), list(flow.starts)
    while frontier:
        for edge in flow.outgoing(frontier.pop()):
            if edge.target not in reached:
                reached.add(edge.target)
                frontier.append(edge.target)
    assert reached == {n.key for n in flow.nodes}


@pytest.mark.parametrize("flow", FLOWS.values(), ids=lambda f: f.title)
def test_only_end_nodes_have_no_way_out(flow):
    for node in flow.nodes:
        assert bool(flow.outgoing(node.key)) == (node.kind != "end"), node.label


@pytest.mark.parametrize("flow", FLOWS.values(), ids=lambda f: f.title)
def test_edge_ids_are_unique(flow):
    ids = [e.id for e in flow.edges]
    assert len(ids) == len(set(ids))


def test_starts_are_diagram_starts_or_clarified():
    for flow in FLOWS.values():
        for start in flow.starts:
            node = flow.node(start)
            assert node.kind == "start" or (flow.number, node.label) in CLARIFIED_STARTS


def test_approval_edges_are_exactly_fr004():
    approval_nodes = {
        (f.number, f.node(e.source).label) for f in FLOWS.values() for e in f.edges if e.approval
    }
    assert approval_nodes == {
        (1, "Committee approves?"),
        (1, "HOD approves requisition?"),
        (1, "Finance approves PO?"),
        (1, "Retrospective approval?"),
        (2, "Pharmacist reviews indent"),
        (4, "Write-off approved?"),
    }


def test_reason_edges_lead_to_fr005_nodes():
    reason_targets = {
        (f.number, f.node(e.target).label) for f in FLOWS.values() for e in f.edges if e.reason
    }
    assert {
        (2, "Return to ward with reason"),
        (1, "Reject: notify requester, suggest formulary alternative"),
        (3, "Query to prescriber"),
        (4, "Investigate loss"),
    } <= reason_targets
