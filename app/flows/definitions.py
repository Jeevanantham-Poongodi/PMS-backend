"""The five flows of docs/flows/Hospital_Pharmacy_System_Flow_v2.pdf as data (research R1).

Every node and arrow of the spec's Branch Inventory (specs/002-flow-pages/spec.md) is listed here
with the diagram's exact label. Node keys are stored on cases, so they never change once used.

Edge options:
- approval: FR-004 applies (flows.approve; raiser may not approve unless demo mode)
- reason:   a note is required (FR-005)
- guard:    the data decides whether the edge is offered (research R2; app.domain.effects)
- effect:   the stock or case effect run when the edge is taken (FR-020 to FR-025)
- inputs:   values the user gives when taking the edge
"""

import re
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
        suffix = _slug(self.outcome) if self.outcome else self.target
        return f"{self.source}__{suffix}"


@dataclass(frozen=True)
class Flow:
    flow: int
    title: str
    starts: tuple[str, ...]
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    _by_key: dict[str, Node] = field(default_factory=dict, compare=False, repr=False)

    def node(self, key: str) -> Node | None:
        if not self._by_key:
            self._by_key.update({n.key: n for n in self.nodes})
        return self._by_key.get(key)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _flow(
    number: int,
    title: str,
    starts: tuple[str, ...],
    ends: tuple[str, ...],
    labels: dict[str, str],
    edges: list[Edge],
) -> Flow:
    outgoing: dict[str, int] = {}
    for edge in edges:
        outgoing[edge.source] = outgoing.get(edge.source, 0) + 1

    def kind(key: str) -> str:
        if key in starts:
            return "start"
        if key in ends:
            return "end"
        if labels[key].endswith("?") or outgoing.get(key, 0) > 1:
            return "decision"
        return "task"

    nodes = tuple(Node(key, label, kind(key)) for key, label in labels.items())
    return Flow(number, title, starts, nodes, tuple(edges))


E = Edge

# ---------------------------------------------------------------------------------------------
# Flow 1 — Procurement
# ---------------------------------------------------------------------------------------------

FLOW_1 = _flow(
    1,
    "Procurement",
    starts=("f1_need", "f1_emergency"),
    ends=("f1_closed", "f1_invoice", "f1_escalate"),
    labels={
        "f1_need": "Stock below reorder level or department need",
        "f1_raise": "Store raises purchase requisition",
        "f1_formulary": "Formulary item?",
        "f1_committee_review": "Drug committee review",
        "f1_committee": "Committee approves?",
        "f1_reject": "Reject: notify requester, suggest formulary alternative",
        "f1_hod": "HOD approves requisition?",
        "f1_revise_req": "Revise requisition",
        "f1_rate_contract": "Rate contract exists?",
        "f1_rfq": "RFQ to vendors, collect quotes",
        "f1_comparative": "Comparative statement, select vendor",
        "f1_create_po": "Create PO",
        "f1_finance": "Finance approves PO?",
        "f1_revise_po": "Revise PO",
        "f1_send_po": "Send PO to vendor",
        "f1_vendor_response": "Vendor response",
        "f1_backorder": "Backorder balance quantity",
        "f1_alternate": "Pick alternate vendor",
        "f1_goods_arrive": "Goods arrive with invoice",
        "f1_grn": "GRN check: qty, batch, expiry, price",
        "f1_return_vendor": "Return to vendor, debit note",
        "f1_qc_quarantine": "Quarantine for QC",
        "f1_qc": "QC passed?",
        "f1_stock_central": "Stock in central store",
        "f1_invoice": "Invoice matched, payment released",
        "f1_closed": "Requisition closed",
        "f1_emergency": "Emergency local purchase",
        "f1_retro": "Retrospective approval?",
        "f1_escalate": "Escalate to audit",
    },
    edges=[
        E("f1_need", "f1_raise"),
        E("f1_raise", "f1_formulary"),
        E("f1_formulary", "f1_hod", "Yes", guard="all_formulary"),
        E("f1_formulary", "f1_committee_review", "No", guard="not_all_formulary"),
        E("f1_committee_review", "f1_committee"),
        E("f1_committee", "f1_hod", "Approved", approval=True),
        E("f1_committee", "f1_reject", "Rejected", approval=True, reason=True),
        E("f1_reject", "f1_closed"),
        E(
            "f1_hod",
            "f1_rate_contract",
            "Approved",
            approval=True,
            effect="approve_qty",
            inputs=("approved_qty",),
        ),
        E("f1_hod", "f1_revise_req", "Rejected", approval=True),
        E("f1_revise_req", "f1_hod", "Resubmit"),
        E("f1_revise_req", "f1_closed", "Drop"),
        E("f1_rate_contract", "f1_create_po", "Yes", effect="set_vendor", inputs=("vendor",)),
        E("f1_rate_contract", "f1_rfq", "No"),
        E("f1_rfq", "f1_comparative"),
        E("f1_comparative", "f1_create_po", effect="set_vendor", inputs=("vendor",)),
        E("f1_create_po", "f1_finance"),
        E("f1_finance", "f1_send_po", "Approved", approval=True),
        E("f1_finance", "f1_revise_po", "Rejected", approval=True),
        E("f1_revise_po", "f1_finance", "Resubmit"),
        E("f1_send_po", "f1_vendor_response"),
        E("f1_vendor_response", "f1_goods_arrive", "Confirmed"),
        E("f1_vendor_response", "f1_backorder", "Partial"),
        E("f1_vendor_response", "f1_alternate", "Declined"),
        E("f1_alternate", "f1_comparative"),
        E("f1_backorder", "f1_goods_arrive", "Balance arrives later"),
        E("f1_goods_arrive", "f1_grn"),
        E("f1_grn", "f1_qc_quarantine", "All OK", effect="grn_receive", inputs=("batches",)),
        # Spec gap U1: a short delivery posts nothing; it is checked again when the balance comes.
        E("f1_grn", "f1_backorder", "Short"),
        E("f1_grn", "f1_return_vendor", "Wrong or damaged"),
        E("f1_return_vendor", "f1_create_po", "Reorder"),
        E("f1_qc_quarantine", "f1_qc"),
        E("f1_qc", "f1_stock_central", "Pass", effect="qc_pass"),
        E("f1_qc", "f1_return_vendor", "Fail", effect="qc_fail"),
        E("f1_stock_central", "f1_invoice"),
        E("f1_emergency", "f1_retro"),
        E(
            "f1_retro",
            "f1_stock_central",
            "Approved",
            approval=True,
            effect="emergency_receive",
            inputs=("batches",),
        ),
        E("f1_retro", "f1_escalate", "Rejected", approval=True),
    ],
)

# ---------------------------------------------------------------------------------------------
# Flow 2 — Ward indent
# ---------------------------------------------------------------------------------------------

FLOW_2 = _flow(
    2,
    "Ward indent",
    starts=("f2_need",),
    ends=("f2_closed",),
    labels={
        "f2_need": "Ward, OT or ICU needs stock",
        "f2_raise": "Ward raises indent to pharmacy",
        "f2_review": "Pharmacist reviews indent",
        "f2_return_ward": "Return to ward with reason",
        "f2_enough": "Enough stock in pharmacy?",
        "f2_requisition": "Raise requisition to central store",
        "f2_issue": "Issue approved quantity, earliest expiry first",
        "f2_receipt": "Ward receipt matches issue?",
        "f2_discrepancy": "Raise discrepancy and reconcile",
        "f2_ward_stock": "Ward stock updated",
        "f2_unused": "Unused or excess ward stock?",
        "f2_return_slip": "Return to pharmacy with return slip",
        "f2_check": "Sealed, unexpired, cold chain OK?",
        "f2_restock": "Restock in pharmacy, credit ward",
        "f2_disposal": "Quarantine for disposal (Flow 4)",
        "f2_closed": "Indent closed",
    },
    edges=[
        E("f2_need", "f2_raise"),
        E("f2_raise", "f2_review"),
        E("f2_review", "f2_return_ward", "Rejected", approval=True, reason=True),
        E("f2_return_ward", "f2_raise", "Revise and resubmit"),
        E(
            "f2_review",
            "f2_enough",
            "Approved (full or partial)",
            approval=True,
            effect="approve_qty",
            inputs=("approved_qty",),
        ),
        E("f2_enough", "f2_issue", "Yes", guard="pharmacy_covers"),
        E(
            "f2_enough",
            "f2_requisition",
            "No",
            guard="pharmacy_short",
            effect="raise_refill_requisition",
        ),
        E("f2_requisition", "f2_issue", "Stock received", effect="refill_pharmacy"),
        E("f2_issue", "f2_receipt", effect="issue_to_ward"),
        E("f2_receipt", "f2_ward_stock", "Yes"),
        E("f2_receipt", "f2_discrepancy", "No"),
        E("f2_discrepancy", "f2_ward_stock"),
        E("f2_ward_stock", "f2_unused"),
        E("f2_unused", "f2_closed", "No"),
        E("f2_unused", "f2_return_slip", "Yes", effect="record_returns", inputs=("return_qty",)),
        E("f2_return_slip", "f2_check"),
        E("f2_check", "f2_restock", "Accepted", effect="ward_return_accept"),
        E("f2_check", "f2_disposal", "Rejected", effect="ward_return_reject"),
        E("f2_restock", "f2_closed"),
        E("f2_disposal", "f2_closed"),
    ],
)

# ---------------------------------------------------------------------------------------------
# Flow 3 — Patient dispensing
# ---------------------------------------------------------------------------------------------

FLOW_3 = _flow(
    3,
    "Patient dispensing",
    starts=("f3_order",),
    ends=("f3_closed",),
    labels={
        "f3_order": "Doctor writes medication order, IP or OP",
        "f3_verify": "Pharmacist verification",
        "f3_query": "Query to prescriber",
        "f3_response": "Prescriber response",
        "f3_in_stock": "In stock?",
        "f3_substitute": "Substitute with prescriber consent or emergency purchase (Flow 1)",
        "f3_controlled": "Narcotic or Schedule X?",
        "f3_ndps": "NDPS register entry, dual sign-off",
        "f3_dispense": "Dispense from earliest-expiry batch",
        "f3_ip_op": "IP or OP?",
        "f3_ip_bill": "Charge to patient bill or TPA",
        "f3_nurse": "Nurse administers",
        "f3_op_bill": "Bill at counter (cash / insurance)",
        "f3_counsel": "Counsel patient, label and hand over",
        "f3_unused": "Unused medicine returned?",
        "f3_check": "Sealed, unexpired, cold chain OK?",
        "f3_restock": "Restock and credit patient",
        "f3_disposal": "Quarantine for disposal (Flow 4)",
        "f3_closed": "Order closed",
    },
    edges=[
        E("f3_order", "f3_verify"),
        E("f3_verify", "f3_query", "Rejected or unclear", reason=True),
        E("f3_query", "f3_response"),
        E(
            "f3_response",
            "f3_verify",
            "Order modified",
            effect="modify_order",
            inputs=("order_lines",),
        ),
        E("f3_response", "f3_closed", "Order cancelled"),
        E("f3_verify", "f3_in_stock", "Approved"),
        E("f3_in_stock", "f3_controlled", "Yes", guard="pharmacy_covers"),
        E("f3_in_stock", "f3_substitute", "No", guard="pharmacy_short"),
        E("f3_substitute", "f3_controlled", effect="substitute", inputs=("substitute",)),
        E("f3_controlled", "f3_ndps", "Yes", guard="has_controlled"),
        E("f3_controlled", "f3_dispense", "No", guard="no_controlled"),
        E("f3_ndps", "f3_dispense", effect="ndps_entry", inputs=("signoffs",)),
        E("f3_dispense", "f3_ip_op", effect="dispense"),
        E("f3_ip_op", "f3_ip_bill", "IP", guard="order_ip"),
        E("f3_ip_bill", "f3_nurse"),
        E("f3_nurse", "f3_unused"),
        E("f3_ip_op", "f3_op_bill", "OP", guard="order_op"),
        E("f3_op_bill", "f3_counsel"),
        E("f3_counsel", "f3_unused"),
        E("f3_unused", "f3_closed", "No"),
        E("f3_unused", "f3_check", "Yes", effect="record_returns", inputs=("return_qty",)),
        E("f3_check", "f3_restock", "Accepted", effect="patient_return_accept"),
        E("f3_check", "f3_disposal", "Rejected", effect="patient_return_reject"),
        E("f3_restock", "f3_closed"),
        E("f3_disposal", "f3_closed"),
    ],
)

# ---------------------------------------------------------------------------------------------
# Flow 4 — Expiry management
# ---------------------------------------------------------------------------------------------

FLOW_4 = _flow(
    4,
    "Expiry management",
    starts=("f4_scan",),
    ends=("f4_updated",),
    labels={
        "f4_scan": "Daily expiry scan",
        "f4_window": "Within 90, 60 or 30 days?",
        "f4_move": "Move to high-use ward or offer return to vendor",
        "f4_vendor": "Vendor accepts return?",
        "f4_credit": "Credit note received",
        "f4_watch": "Keep on near-expiry watch list, flag for FEFO priority issue",
        "f4_used": "Used before expiry date?",
        "f4_expired": "Expired: quarantine",
        "f4_writeoff": "Write-off approved?",
        "f4_dispose": "Dispose via biomedical waste vendor",
        "f4_investigate": "Investigate loss",
        "f4_updated": "Stock records updated",
    },
    edges=[
        E("f4_scan", "f4_window"),
        E("f4_window", "f4_updated", "No", guard="outside_window"),
        E("f4_window", "f4_move", "Yes", guard="within_window"),
        E("f4_move", "f4_vendor", effect="expiry_move", inputs=("move_choice",)),
        # A move to a ward does not ask the vendor, so only No is offered then (FR-024).
        E("f4_vendor", "f4_credit", "Yes", guard="offered_to_vendor"),
        E("f4_credit", "f4_updated", effect="expiry_vendor_return"),
        E("f4_vendor", "f4_watch", "No"),
        E("f4_watch", "f4_used"),
        E("f4_used", "f4_updated", "Yes"),
        E("f4_used", "f4_expired", "No, expired", effect="expiry_quarantine"),
        E("f4_expired", "f4_writeoff"),
        E("f4_writeoff", "f4_dispose", "Approved", approval=True),
        E("f4_dispose", "f4_updated", effect="write_off"),
        E("f4_writeoff", "f4_investigate", "Rejected", approval=True, reason=True),
        E("f4_investigate", "f4_writeoff", "Resubmit"),
    ],
)

# ---------------------------------------------------------------------------------------------
# Flow 5 — Recall
# ---------------------------------------------------------------------------------------------

FLOW_5 = _flow(
    5,
    "Recall",
    starts=("f5_notice",),
    ends=("f5_updated",),
    labels={
        "f5_notice": "Recall notice from regulator or vendor",
        "f5_find": "Find affected batches in all stores and wards",
        "f5_block": "Block and quarantine batches",
        "f5_dispensed": "Already dispensed to patients?",
        "f5_inform": "Inform prescribers, trace patients",
        "f5_return": "Return to vendor for credit or replacement",
        "f5_updated": "Stock records updated",
    },
    edges=[
        E("f5_notice", "f5_find", effect="recall_find"),
        E("f5_find", "f5_block", effect="recall_block"),
        E("f5_block", "f5_dispensed"),
        E("f5_dispensed", "f5_inform", "Yes", guard="batch_dispensed"),
        E("f5_inform", "f5_return"),
        E("f5_dispensed", "f5_return", "No", guard="batch_not_dispensed"),
        E("f5_return", "f5_updated", effect="recall_return"),
    ],
)

FLOWS: dict[int, Flow] = {f.flow: f for f in (FLOW_1, FLOW_2, FLOW_3, FLOW_4, FLOW_5)}

# Nodes where linked cases are opened by other flows' effects, not from the API:
# a rejected return goes to F4:"Expired: quarantine" (FR-021, FR-022).
INTERNAL_STARTS: dict[int, tuple[str, ...]] = {4: ("f4_expired",)}


def get_flow(number: int) -> Flow:
    return FLOWS[number]


def outgoing(flow: int, node: str) -> list[Edge]:
    return [e for e in FLOWS[flow].edges if e.source == node]


def is_end(flow: int, node: str) -> bool:
    found = FLOWS[flow].node(node)
    return found is not None and found.kind == "end"
