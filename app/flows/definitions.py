"""The five flows of docs/flows/Hospital_Pharmacy_System_Flow_v2.pdf as state machines.

This is the Branch Inventory of specs/002-flow-pages/spec.md encoded as data: every node is a
case state with the diagram's exact label, every arrow an edge with the diagram's outcome label.
The API serves these definitions so the UI renders from the same source (research R1).

Edge options:
- approval: FR-004 (raiser may not take it unless demo mode)
- reason: FR-005 (a note is required)
- guard: availability decided by data, not by the user (research R2); see domain/effects.py
- effect: stock or linked-case effect; see domain/effects.py
- inputs: what the user must enter when taking the edge
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    key: str
    label: str
    kind: str  # start | task | decision | end


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    outcome: str | None = None
    approval: bool = False
    reason: bool = False
    guard: str | None = None
    effect: str | None = None
    inputs: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        slug = (self.outcome or "next").lower()
        slug = "".join(c if c.isalnum() else "_" for c in slug).strip("_")
        return f"{self.source}__{slug}"


@dataclass(frozen=True)
class Flow:
    number: int
    title: str
    starts: tuple[str, ...]
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    node_index: dict[str, Node] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_index", {n.key: n for n in self.nodes})

    def node(self, key: str) -> Node:
        return self.node_index[key]

    def outgoing(self, key: str) -> list[Edge]:
        return [e for e in self.edges if e.source == key]

    def edge(self, edge_id: str) -> Edge | None:
        return next((e for e in self.edges if e.id == edge_id), None)


def _n(key: str, label: str, kind: str = "task") -> Node:
    return Node(key, label, kind)


# ---------------------------------------------------------------------------
# Flow 1 — Procurement: purchase requisition to central store
# ---------------------------------------------------------------------------
PROCUREMENT = Flow(
    1,
    "Procurement",
    starts=("f1_need", "f1_emergency"),
    nodes=(
        _n("f1_need", "Stock below reorder level or department need", "start"),
        _n("f1_raise", "Store raises purchase requisition"),
        _n("f1_formulary", "Formulary item?", "decision"),
        _n("f1_committee_review", "Drug committee review"),
        _n("f1_committee", "Committee approves?", "decision"),
        _n("f1_reject", "Reject: notify requester, suggest formulary alternative"),
        _n("f1_hod", "HOD approves requisition?", "decision"),
        _n("f1_revise", "Revise requisition"),
        _n("f1_rate_contract", "Rate contract exists?", "decision"),
        _n("f1_rfq", "RFQ to vendors, collect quotes"),
        _n("f1_comparative", "Comparative statement, select vendor"),
        _n("f1_create_po", "Create PO"),
        _n("f1_finance", "Finance approves PO?", "decision"),
        _n("f1_revise_po", "Revise PO"),
        _n("f1_send_po", "Send PO to vendor"),
        _n("f1_vendor_response", "Vendor response", "decision"),
        _n("f1_alternate_vendor", "Pick alternate vendor"),
        _n("f1_backorder", "Backorder balance quantity"),
        _n("f1_goods_arrive", "Goods arrive with invoice"),
        _n("f1_grn", "GRN check: qty, batch, expiry, price", "decision"),
        _n("f1_return_vendor", "Return to vendor, debit note"),
        _n("f1_qc_quarantine", "Quarantine for QC"),
        _n("f1_qc", "QC passed?", "decision"),
        _n("f1_stock_central", "Stock in central store"),
        _n("f1_invoice", "Invoice matched, payment released", "end"),
        _n("f1_closed", "Requisition closed", "end"),
        _n("f1_emergency", "Emergency local purchase", "start"),
        _n("f1_retro", "Retrospective approval?", "decision"),
        _n("f1_audit", "Escalate to audit", "end"),
    ),
    edges=(
        Edge("f1_need", "f1_raise"),
        Edge("f1_raise", "f1_formulary"),
        Edge("f1_formulary", "f1_hod", "Yes", guard="formulary"),
        Edge("f1_formulary", "f1_committee_review", "No", guard="non_formulary"),
        Edge("f1_committee_review", "f1_committee"),
        Edge("f1_committee", "f1_hod", "Approved", approval=True),
        Edge("f1_committee", "f1_reject", "Rejected", approval=True, reason=True),
        Edge("f1_reject", "f1_closed"),
        Edge(
            "f1_hod",
            "f1_rate_contract",
            "Approved",
            approval=True,
            effect="approve_qty",
            inputs=("approved_qty",),
        ),
        Edge("f1_hod", "f1_revise", "Rejected", approval=True),
        Edge("f1_revise", "f1_hod", "Resubmit"),
        Edge("f1_revise", "f1_closed", "Drop"),
        Edge("f1_rate_contract", "f1_create_po", "Yes", effect="set_vendor", inputs=("vendor",)),
        Edge("f1_rate_contract", "f1_rfq", "No"),
        Edge("f1_rfq", "f1_comparative"),
        Edge("f1_comparative", "f1_create_po", effect="set_vendor", inputs=("vendor",)),
        Edge("f1_create_po", "f1_finance"),
        Edge("f1_finance", "f1_send_po", "Approved", approval=True),
        Edge("f1_finance", "f1_revise_po", "Rejected", approval=True),
        Edge("f1_revise_po", "f1_finance", "Resubmit"),
        Edge("f1_send_po", "f1_vendor_response"),
        Edge("f1_vendor_response", "f1_goods_arrive", "Confirmed"),
        Edge("f1_vendor_response", "f1_backorder", "Partial"),
        Edge("f1_vendor_response", "f1_alternate_vendor", "Declined"),
        Edge("f1_alternate_vendor", "f1_comparative"),
        Edge("f1_backorder", "f1_goods_arrive", "Balance arrives later"),
        Edge("f1_goods_arrive", "f1_grn"),
        Edge("f1_grn", "f1_qc_quarantine", "All OK", effect="grn_receive", inputs=("batches",)),
        # Short posts nothing; the whole delivery is checked again when the balance arrives.
        Edge("f1_grn", "f1_backorder", "Short"),
        Edge("f1_grn", "f1_return_vendor", "Wrong or damaged"),
        Edge("f1_return_vendor", "f1_create_po", "Reorder"),
        Edge("f1_qc_quarantine", "f1_qc"),
        Edge("f1_qc", "f1_stock_central", "Pass", effect="qc_pass"),
        Edge("f1_qc", "f1_return_vendor", "Fail", effect="qc_fail"),
        Edge("f1_stock_central", "f1_invoice"),
        Edge("f1_emergency", "f1_retro"),
        Edge(
            "f1_retro",
            "f1_stock_central",
            "Approved",
            approval=True,
            effect="emergency_receive",
            inputs=("batches",),
        ),
        Edge("f1_retro", "f1_audit", "Rejected", approval=True),
    ),
)

# ---------------------------------------------------------------------------
# Flow 2 — Ward indent: pharmacy to ward, OT or ICU (with ward returns)
# ---------------------------------------------------------------------------
WARD_INDENT = Flow(
    2,
    "Ward indent",
    starts=("f2_need",),
    nodes=(
        _n("f2_need", "Ward, OT or ICU needs stock", "start"),
        _n("f2_raise", "Ward raises indent to pharmacy"),
        _n("f2_review", "Pharmacist reviews indent", "decision"),
        _n("f2_return_ward", "Return to ward with reason"),
        _n("f2_enough", "Enough stock in pharmacy?", "decision"),
        _n("f2_requisition", "Raise requisition to central store"),
        _n("f2_issue", "Issue approved quantity, earliest expiry first"),
        _n("f2_receipt", "Ward receipt matches issue?", "decision"),
        _n("f2_discrepancy", "Raise discrepancy and reconcile"),
        _n("f2_ward_stock", "Ward stock updated"),
        _n("f2_unused", "Unused or excess ward stock?", "decision"),
        _n("f2_return_slip", "Return to pharmacy with return slip"),
        _n("f2_check", "Sealed, unexpired, cold chain OK?", "decision"),
        _n("f2_restock", "Restock in pharmacy, credit ward"),
        _n("f2_disposal", "Quarantine for disposal (Flow 4)"),
        _n("f2_closed", "Indent closed", "end"),
    ),
    edges=(
        Edge("f2_need", "f2_raise"),
        Edge("f2_raise", "f2_review"),
        Edge("f2_review", "f2_return_ward", "Rejected", approval=True, reason=True),
        Edge("f2_return_ward", "f2_raise", "Revise and resubmit"),
        Edge(
            "f2_review",
            "f2_enough",
            "Approved (full or partial)",
            approval=True,
            effect="approve_qty",
            inputs=("approved_qty",),
        ),
        Edge("f2_enough", "f2_issue", "Yes", guard="pharmacy_covers"),
        Edge(
            "f2_enough",
            "f2_requisition",
            "No",
            guard="pharmacy_short",
            effect="raise_refill_requisition",
        ),
        Edge("f2_requisition", "f2_issue", "Stock received", effect="refill_pharmacy"),
        Edge("f2_issue", "f2_receipt", effect="issue_to_ward"),
        Edge("f2_receipt", "f2_ward_stock", "Yes"),
        Edge("f2_receipt", "f2_discrepancy", "No", reason=True),
        Edge("f2_discrepancy", "f2_ward_stock"),
        Edge("f2_ward_stock", "f2_unused"),
        Edge("f2_unused", "f2_closed", "No"),
        Edge(
            "f2_unused", "f2_return_slip", "Yes", effect="record_return_qty", inputs=("return_qty",)
        ),
        Edge("f2_return_slip", "f2_check"),
        Edge("f2_check", "f2_restock", "Accepted", effect="ward_return_accept"),
        Edge("f2_check", "f2_disposal", "Rejected", effect="ward_return_reject"),
        Edge("f2_restock", "f2_closed"),
        Edge("f2_disposal", "f2_closed"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 3 — Patient dispensing: inpatient (IP) and outpatient (OP) orders
# ---------------------------------------------------------------------------
DISPENSING = Flow(
    3,
    "Patient dispensing",
    starts=("f3_order",),
    nodes=(
        _n("f3_order", "Doctor writes medication order, IP or OP", "start"),
        _n("f3_verify", "Pharmacist verification", "decision"),
        _n("f3_query", "Query to prescriber"),
        _n("f3_response", "Prescriber response", "decision"),
        _n("f3_in_stock", "In stock?", "decision"),
        _n("f3_substitute", "Substitute with prescriber consent or emergency purchase (Flow 1)"),
        _n("f3_controlled", "Narcotic or Schedule X?", "decision"),
        _n("f3_ndps", "NDPS register entry, dual sign-off"),
        _n("f3_dispense", "Dispense from earliest-expiry batch"),
        _n("f3_ip_op", "IP or OP?", "decision"),
        _n("f3_ip_bill", "Charge to patient bill or TPA"),
        _n("f3_nurse", "Nurse administers"),
        _n("f3_op_bill", "Bill at counter (cash / insurance)"),
        _n("f3_counsel", "Counsel patient, label and hand over"),
        _n("f3_unused", "Unused medicine returned?", "decision"),
        _n("f3_check", "Sealed, unexpired, cold chain OK?", "decision"),
        _n("f3_restock", "Restock and credit patient"),
        _n("f3_disposal", "Quarantine for disposal (Flow 4)"),
        _n("f3_closed", "Order closed", "end"),
    ),
    edges=(
        Edge("f3_order", "f3_verify"),
        Edge("f3_verify", "f3_query", "Rejected or unclear", reason=True),
        Edge("f3_query", "f3_response"),
        Edge(
            "f3_response",
            "f3_verify",
            "Order modified",
            effect="modify_order",
            inputs=("order_lines",),
        ),
        Edge("f3_response", "f3_closed", "Order cancelled"),
        Edge("f3_verify", "f3_in_stock", "Approved"),
        Edge("f3_in_stock", "f3_controlled", "Yes", guard="pharmacy_covers"),
        Edge("f3_in_stock", "f3_substitute", "No", guard="pharmacy_short"),
        Edge(
            "f3_substitute",
            "f3_controlled",
            effect="substitute_or_purchase",
            inputs=("substitute",),
        ),
        Edge("f3_controlled", "f3_ndps", "Yes", guard="controlled"),
        Edge("f3_controlled", "f3_dispense", "No", guard="not_controlled"),
        Edge("f3_ndps", "f3_dispense", effect="ndps_entry", inputs=("signoffs",)),
        Edge("f3_dispense", "f3_ip_op", effect="dispense"),
        Edge("f3_ip_op", "f3_ip_bill", "IP", guard="order_ip"),
        Edge("f3_ip_bill", "f3_nurse"),
        Edge("f3_nurse", "f3_unused"),
        Edge("f3_ip_op", "f3_op_bill", "OP", guard="order_op"),
        Edge("f3_op_bill", "f3_counsel"),
        Edge("f3_counsel", "f3_unused"),
        Edge("f3_unused", "f3_closed", "No"),
        Edge("f3_unused", "f3_check", "Yes", effect="record_return_qty", inputs=("return_qty",)),
        Edge("f3_check", "f3_restock", "Accepted", effect="patient_return_accept"),
        Edge("f3_check", "f3_disposal", "Rejected", effect="patient_return_reject"),
        Edge("f3_restock", "f3_closed"),
        Edge("f3_disposal", "f3_closed"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 4 — Expiry management
# ---------------------------------------------------------------------------
EXPIRY = Flow(
    4,
    "Expiry management",
    starts=("f4_scan", "f4_expired"),
    nodes=(
        _n("f4_scan", "Daily expiry scan", "start"),
        _n("f4_window", "Within 90, 60 or 30 days?", "decision"),
        _n("f4_move", "Move to high-use ward or offer return to vendor"),
        _n("f4_vendor", "Vendor accepts return?", "decision"),
        _n("f4_credit", "Credit note received"),
        _n("f4_watch", "Keep on near-expiry watch list, flag for FEFO priority issue"),
        _n("f4_used", "Used before expiry date?", "decision"),
        _n("f4_expired", "Expired: quarantine"),
        _n("f4_writeoff", "Write-off approved?", "decision"),
        _n("f4_investigate", "Investigate loss"),
        _n("f4_dispose", "Dispose via biomedical waste vendor"),
        _n("f4_updated", "Stock records updated", "end"),
    ),
    edges=(
        Edge("f4_scan", "f4_window"),
        Edge("f4_window", "f4_updated", "No", guard="outside_window"),
        Edge("f4_window", "f4_move", "Yes", guard="within_window"),
        Edge("f4_move", "f4_vendor", effect="move_or_offer", inputs=("move_choice",)),
        Edge(
            "f4_vendor",
            "f4_credit",
            "Yes",
            guard="offered_to_vendor",
            effect="expiry_return_to_vendor",
        ),
        Edge("f4_credit", "f4_updated"),
        Edge("f4_vendor", "f4_watch", "No"),
        Edge("f4_watch", "f4_used"),
        Edge("f4_used", "f4_updated", "Yes", guard="stock_used"),
        Edge(
            "f4_used", "f4_expired", "No, expired", guard="stock_left", effect="expiry_quarantine"
        ),
        Edge("f4_expired", "f4_writeoff"),
        Edge("f4_writeoff", "f4_dispose", "Approved", approval=True),
        Edge("f4_writeoff", "f4_investigate", "Rejected", approval=True, reason=True),
        Edge("f4_investigate", "f4_writeoff", "Resubmit"),
        Edge("f4_dispose", "f4_updated", effect="dispose"),
    ),
)

# ---------------------------------------------------------------------------
# Flow 5 — Recall
# ---------------------------------------------------------------------------
RECALL = Flow(
    5,
    "Recall",
    starts=("f5_notice",),
    nodes=(
        _n("f5_notice", "Recall notice from regulator or vendor", "start"),
        _n("f5_find", "Find affected batches in all stores and wards"),
        _n("f5_block", "Block and quarantine batches"),
        _n("f5_dispensed", "Already dispensed to patients?", "decision"),
        _n("f5_inform", "Inform prescribers, trace patients"),
        _n("f5_return", "Return to vendor for credit or replacement"),
        _n("f5_updated", "Stock records updated", "end"),
    ),
    edges=(
        Edge("f5_notice", "f5_find", effect="find_affected"),
        Edge("f5_find", "f5_block", effect="block_and_quarantine"),
        Edge("f5_block", "f5_dispensed"),
        Edge("f5_dispensed", "f5_inform", "Yes", guard="dispensed"),
        Edge("f5_dispensed", "f5_return", "No", guard="not_dispensed"),
        Edge("f5_inform", "f5_return"),
        Edge("f5_return", "f5_updated", effect="recall_return_to_vendor"),
    ),
)

FLOWS: dict[int, Flow] = {
    f.number: f for f in (PROCUREMENT, WARD_INDENT, DISPENSING, EXPIRY, RECALL)
}


def get_flow(number: int) -> Flow | None:
    return FLOWS.get(number)
