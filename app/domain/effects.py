"""What each diagram arrow does beyond moving the case: guards decide which arrows the data
allows (research R2); effects post stock and record details (FR-020 to FR-025).

Both receive a Ctx describing the case being moved. Effects run inside the move's transaction,
so a refusal here (AppError) rolls back the whole move.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.core.errors import AppError
from app.domain import stock
from app.flows.definitions import Edge
from app.repositories import audit
from app.repositories import cases as cases_repo
from app.repositories import masters as masters_repo
from app.repositories import stock as stock_repo

ZERO = Decimal(0)
WARD_KINDS = ("ward", "ot", "icu")


@dataclass
class Ctx:
    cur: Cursor
    user: CurrentUser
    case: dict
    lines: list[dict]
    data: dict
    edge: Edge | None = None
    input: dict = field(default_factory=dict)
    note: str | None = None
    request_id: str | None = None
    demo_mode: bool = False

    @property
    def hospital_id(self) -> int:
        return self.user.hospital_id

    def location(self, kind: str) -> int:
        found = masters_repo.location_of_kind(self.cur, self.hospital_id, kind)
        if found is None:
            raise AppError(409, "LOCATION_MISSING", f"No {kind.replace('_', ' ')} location.")
        return found["id"]

    def post(self, doc_type: str, lines: list[dict], **kwargs: Any) -> int | None:
        return stock.post_document(
            self.cur,
            self.hospital_id,
            doc_type,
            lines,
            case_id=self.case["id"],
            request_id=self.request_id,
            **kwargs,
        )

    def items(self) -> dict[int, dict]:
        item_ids = [ln["item_id"] for ln in self.lines]
        return masters_repo.get_items(self.cur, self.hospital_id, item_ids)

    def update_line(self, line: dict, **changes: Any) -> None:
        cases_repo.update_line(self.cur, self.hospital_id, line["id"], changes)
        line.update(changes)


def line_qty(line: dict) -> Decimal:
    """The quantity a line now stands for: approved if an approval set it, else requested."""
    value = line["qty_approved"] if line["qty_approved"] is not None else line["qty_requested"]
    return Decimal(value)


def needed_by_item(lines: list[dict]) -> dict[int, Decimal]:
    needed: dict[int, Decimal] = {}
    for line in lines:
        qty = line_qty(line)
        if qty > ZERO:
            needed[line["item_id"]] = needed.get(line["item_id"], ZERO) + qty
    return needed


def invalid(message: str, field_name: str | None = None) -> AppError:
    details = [{"field": field_name, "message": message}] if field_name else None
    return AppError(422, "VALIDATION_FAILED", message, details)


def qty_map(ctx: Ctx, key: str) -> dict[int, Decimal]:
    """Reads {line_id: quantity} from the action input; every key must be a line of the case."""
    raw = ctx.input.get(key)
    if not isinstance(raw, dict):
        raise invalid("Enter a quantity for each line.", key)
    by_id = {ln["id"]: ln for ln in ctx.lines}
    values: dict[int, Decimal] = {}
    for line_id, value in raw.items():
        try:
            lid = int(line_id)
            qty = stock.to_qty(value if value not in ("", None) else 0)
        except (ValueError, InvalidOperation) as exc:
            raise invalid("Quantities must be numbers.", key) from exc
        if lid not in by_id:
            raise invalid("A quantity refers to a line that is not on this case.", key)
        if qty < ZERO:
            raise invalid("Quantities cannot be negative.", key)
        values[lid] = qty
    return values


def flt(value: Decimal) -> float:
    return float(value)


# ------------------------------------------------------------------------------------------------
# Guards: decision nodes answered by data (research R2)
# ------------------------------------------------------------------------------------------------


def _all_formulary(ctx: Ctx) -> bool:
    items = ctx.items()
    return all(items[ln["item_id"]]["formulary_status"] == "formulary" for ln in ctx.lines)


def _pharmacy_covers(ctx: Ctx) -> bool:
    pharmacy = ctx.location("pharmacy")
    return all(
        stock.available(ctx.cur, ctx.hospital_id, item_id, pharmacy) >= qty
        for item_id, qty in needed_by_item(ctx.lines).items()
    )


def _has_controlled(ctx: Ctx) -> bool:
    return any(item["controlled_class"] != "none" for item in ctx.items().values())


def _within_window(ctx: Ctx) -> bool:
    batch = masters_repo.get_batch(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"]))
    windows = masters_repo.get_setting(
        ctx.cur, ctx.hospital_id, "expiry.alert_windows_days", [90, 60, 30]
    )
    days = (batch["expiry_date"] - hospital_today(ctx.cur, ctx.hospital_id)).days
    return days <= max(windows)


def _batch_dispensed(ctx: Ctx) -> bool:
    return bool(stock_repo.batch_dispensed(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"])))


GUARDS: dict[str, Callable[[Ctx], bool]] = {
    "all_formulary": _all_formulary,
    "not_all_formulary": lambda ctx: not _all_formulary(ctx),
    "pharmacy_covers": _pharmacy_covers,
    "pharmacy_short": lambda ctx: not _pharmacy_covers(ctx),
    "has_controlled": _has_controlled,
    "no_controlled": lambda ctx: not _has_controlled(ctx),
    "order_ip": lambda ctx: ctx.data.get("order_type") == "IP",
    "order_op": lambda ctx: ctx.data.get("order_type") == "OP",
    "within_window": _within_window,
    "outside_window": lambda ctx: not _within_window(ctx),
    "offered_to_vendor": lambda ctx: ctx.data.get("choice") == "vendor",
    "batch_dispensed": _batch_dispensed,
    "batch_not_dispensed": lambda ctx: not _batch_dispensed(ctx),
}


# ------------------------------------------------------------------------------------------------
# Shared effects
# ------------------------------------------------------------------------------------------------


def approve_qty(ctx: Ctx) -> None:
    """F1:"HOD approves requisition?" / F2:"Pharmacist reviews indent": full or partial."""
    approved = qty_map(ctx, "approved_qty")
    for line in ctx.lines:
        qty = approved.get(line["id"], Decimal(line["qty_requested"]))
        if qty > Decimal(line["qty_requested"]):
            raise invalid("An approved quantity cannot exceed the requested quantity.")
        ctx.update_line(line, qty_approved=qty)
    if all(line_qty(ln) == ZERO for ln in ctx.lines):
        raise invalid("Approve a quantity on at least one line, or reject instead.")


def record_returns(ctx: Ctx) -> None:
    """F2:"Unused or excess ward stock?" / F3:"Unused medicine returned?" Yes: the return slip."""
    returned = qty_map(ctx, "return_qty")
    issued = ctx.data.get("issued", {})
    returns: dict[str, float] = {}
    for line_id, qty in returned.items():
        if qty == ZERO:
            continue
        given = sum((Decimal(str(p["qty"])) for p in issued.get(str(line_id), [])), ZERO)
        if qty > given:
            raise invalid("A returned quantity cannot exceed what was issued.", "return_qty")
        returns[str(line_id)] = flt(qty)
    if not returns:
        raise invalid("Enter the quantity returned on at least one line.", "return_qty")
    ctx.data["returns"] = returns


def _returned_picks(ctx: Ctx, line: dict) -> list[stock.Pick]:
    """The batches a return comes back in: the issued batches, in the order they were issued."""
    remaining = Decimal(str(ctx.data.get("returns", {}).get(str(line["id"]), 0)))
    picks: list[stock.Pick] = []
    for issued in ctx.data.get("issued", {}).get(str(line["id"]), []):
        if remaining <= ZERO:
            break
        take = min(Decimal(str(issued["qty"])), remaining)
        picks.append(stock.Pick(int(issued["batch_id"]), take))
        remaining -= take
    return picks


def _record_issue(ctx: Ctx, line: dict, picks: list[stock.Pick], location_id: int) -> None:
    ctx.data.setdefault("issued", {})[str(line["id"])] = [
        {"batch_id": p.batch_id, "qty": flt(p.qty)} for p in picks
    ]
    if picks:
        ctx.update_line(line, batch_id=picks[0].batch_id, location_id=location_id)


def _open_linked(ctx: Ctx, flow: int, start: str, title: str, data: dict, lines: list[dict]) -> int:
    from app.domain.flows import open_case  # the engine imports this module

    case_id = open_case(
        ctx.cur,
        ctx.user,
        flow=flow,
        start=start,
        title=title,
        data=data,
        lines=lines,
        parent_case_id=ctx.case["id"],
        request_id=ctx.request_id,
    )
    ctx.data.setdefault("linked_case_ids", []).append(case_id)
    return case_id


def _quarantine_for_disposal(ctx: Ctx, moved: list[dict], what: str) -> None:
    """Opens one linked Flow 4 case at F4:"Expired: quarantine" per batch moved to expiry
    quarantine (FR-021, FR-022)."""
    quarantine = ctx.location("quarantine_expiry")
    for row in moved:
        batch = masters_repo.get_batch(ctx.cur, ctx.hospital_id, row["batch_id"])
        _open_linked(
            ctx,
            4,
            "f4_expired",
            f"{batch['item_name']} {batch['batch_no']} rejected on return ({what})",
            {"batch_id": row["batch_id"], "location_id": quarantine},
            [{
                "item_id": row["item_id"],
                "batch_id": row["batch_id"],
                "location_id": quarantine,
                "qty": row["qty"],
                "qty_approved": row["qty"],
            }],
        )


# ------------------------------------------------------------------------------------------------
# Flow 1 — Procurement (FR-020)
# ------------------------------------------------------------------------------------------------


def set_vendor(ctx: Ctx) -> None:
    raw = ctx.input.get("vendor_id")
    if raw in (None, ""):
        raise invalid("Choose the vendor.", "vendor_id")
    if masters_repo.get_vendor(ctx.cur, ctx.hospital_id, int(raw)) is None:
        raise invalid("Unknown vendor.", "vendor_id")
    ctx.data["vendor_id"] = int(raw)


def _batches_input(ctx: Ctx) -> dict[int, tuple[str, date]]:
    raw = ctx.input.get("batches")
    if not isinstance(raw, dict):
        raise invalid("Enter the batch number and expiry date for each line.", "batches")
    today = hospital_today(ctx.cur, ctx.hospital_id)
    found: dict[int, tuple[str, date]] = {}
    for line in ctx.lines:
        if line_qty(line) == ZERO:
            continue
        entry = raw.get(str(line["id"])) or {}
        batch_no = str(entry.get("batch_no") or "").strip()
        try:
            expiry = date.fromisoformat(str(entry.get("expiry_date") or ""))
        except ValueError as exc:
            raise invalid(f"Enter the expiry date for {line['item_name']}.", "batches") from exc
        if not batch_no:
            raise invalid(f"Enter the batch number for {line['item_name']}.", "batches")
        if expiry <= today:
            raise invalid(f"Batch {batch_no} has already expired.", "batches")
        found[line["id"]] = (batch_no, expiry)
    return found


def _receive(ctx: Ctx, location_kind: str) -> None:
    target = ctx.location(location_kind)
    vendor_id = ctx.data.get("vendor_id")
    ledger: list[dict] = []
    for line_id, (batch_no, expiry) in _batches_input(ctx).items():
        line = next(ln for ln in ctx.lines if ln["id"] == line_id)
        batch = masters_repo.get_or_create_batch(
            ctx.cur, ctx.hospital_id, line["item_id"], batch_no, expiry, vendor_id
        )
        if batch["expiry_date"] != expiry:
            raise invalid(f"Batch {batch_no} is already recorded with another expiry date.")
        qty = line_qty(line)
        ledger.append(
            {"item_id": line["item_id"], "batch_id": batch["id"], "location_id": target,
             "qty": qty}
        )
        ctx.update_line(line, batch_id=batch["id"], location_id=target)
    ctx.post("receipt", ledger, note=ctx.note)


def grn_receive(ctx: Ctx) -> None:
    """F1:"GRN check" All OK: receive into QC quarantine (not allocatable)."""
    _receive(ctx, "quarantine_qc")


def emergency_receive(ctx: Ctx) -> None:
    """F1:"Retrospective approval?" Approved: the emergency purchase enters the central store."""
    _receive(ctx, "central_store")


def _received_lines(ctx: Ctx) -> list[dict]:
    return [ln for ln in ctx.lines if ln["batch_id"] is not None and line_qty(ln) > ZERO]


def qc_pass(ctx: Ctx) -> None:
    """F1:"QC passed?" Pass: QC quarantine → central store."""
    central = ctx.location("central_store")
    ledger: list[dict] = []
    for line in _received_lines(ctx):
        ledger += stock.move(
            line["item_id"], line["batch_id"], line["location_id"], central, line_qty(line)
        )
        ctx.update_line(line, location_id=central)
    ctx.post("transfer", ledger, note=ctx.note)


def qc_fail(ctx: Ctx) -> None:
    """F1:"QC passed?" Fail: the received stock goes back to the vendor."""
    ledger = [
        {"item_id": ln["item_id"], "batch_id": ln["batch_id"], "location_id": ln["location_id"],
         "qty": -line_qty(ln)}
        for ln in _received_lines(ctx)
    ]
    ctx.post("return_to_vendor", ledger, note=ctx.note)


# ------------------------------------------------------------------------------------------------
# Flow 2 — Ward indent (FR-021, FR-021a)
# ------------------------------------------------------------------------------------------------


def _shortfalls(ctx: Ctx, location_id: int) -> dict[int, Decimal]:
    short: dict[int, Decimal] = {}
    for item_id, qty in needed_by_item(ctx.lines).items():
        missing = qty - stock.available(ctx.cur, ctx.hospital_id, item_id, location_id)
        if missing > ZERO:
            short[item_id] = missing
    return short


def raise_refill_requisition(ctx: Ctx) -> None:
    """F2:"Enough stock in pharmacy?" No: a linked Flow 1 requisition for the shortfall."""
    short = _shortfalls(ctx, ctx.location("pharmacy"))
    _open_linked(
        ctx,
        1,
        "f1_need",
        f"Refill for indent #{ctx.case['id']}",
        {"trigger": "department_need"},
        [{"item_id": item_id, "qty": qty} for item_id, qty in short.items()],
    )


def _refill_from_central(ctx: Ctx, short: dict[int, Decimal]) -> None:
    pharmacy, central = ctx.location("pharmacy"), ctx.location("central_store")
    ledger: list[dict] = []
    for item_id, qty in short.items():
        moves, _ = stock.fefo_moves(
            ctx.cur, ctx.hospital_id, item_id, central, pharmacy, qty, "in the central store"
        )
        ledger += moves
    ctx.post("transfer", ledger, note="Refill from central store")


def refill_pharmacy(ctx: Ctx) -> None:
    """F2:"Raise requisition to central store" Stock received: central → pharmacy by FEFO;
    refused while the central store is short (FR-021a)."""
    _refill_from_central(ctx, _shortfalls(ctx, ctx.location("pharmacy")))


def issue_to_ward(ctx: Ctx) -> None:
    """F2:"Issue approved quantity, earliest expiry first": pharmacy → ward by FEFO."""
    pharmacy = ctx.location("pharmacy")
    ward = int(ctx.data["ward_location_id"])
    ledger: list[dict] = []
    for line in ctx.lines:
        qty = line_qty(line)
        if qty == ZERO:
            continue
        moves, picks = stock.fefo_moves(
            ctx.cur, ctx.hospital_id, line["item_id"], pharmacy, ward, qty, "in the pharmacy"
        )
        ledger += moves
        _record_issue(ctx, line, picks, ward)
    ctx.post("issue", ledger, note=ctx.note)


def _ward_returns(ctx: Ctx, target: int) -> list[dict]:
    ward = int(ctx.data["ward_location_id"])
    ledger: list[dict] = []
    for line in ctx.lines:
        for pick in _returned_picks(ctx, line):
            ledger += stock.move(line["item_id"], pick.batch_id, ward, target, pick.qty)
    return ledger


def ward_return_accept(ctx: Ctx) -> None:
    """F2:"Sealed, unexpired, cold chain OK?" Accepted: ward → pharmacy."""
    ctx.post("return", _ward_returns(ctx, ctx.location("pharmacy")), note=ctx.note)


def ward_return_reject(ctx: Ctx) -> None:
    """Rejected: ward → expiry quarantine, and a linked Flow 4 case for disposal."""
    quarantine = ctx.location("quarantine_expiry")
    ledger = _ward_returns(ctx, quarantine)
    ctx.post("transfer", ledger, note=ctx.note)
    _quarantine_for_disposal(
        ctx, [ln for ln in ledger if ln["qty"] > ZERO], f"indent #{ctx.case['id']}"
    )


# ------------------------------------------------------------------------------------------------
# Flow 3 — Patient dispensing (FR-022, FR-023)
# ------------------------------------------------------------------------------------------------


def modify_order(ctx: Ctx) -> None:
    """F3:"Prescriber response" Order modified: new quantities per line."""
    changed = qty_map(ctx, "order_lines")
    for line in ctx.lines:
        qty = changed.get(line["id"])
        if qty is not None:
            ctx.update_line(line, qty_requested=qty, qty_approved=None)
    if all(line_qty(ln) == ZERO for ln in ctx.lines):
        raise invalid("A modified order needs a quantity on at least one line.", "order_lines")


def substitute(ctx: Ctx) -> None:
    """F3:"Substitute with prescriber consent or emergency purchase (Flow 1)" (spec gap: the user
    records which; an emergency purchase opens a linked Flow 1 case)."""
    raw = ctx.input.get("substitute") or {}
    choice = raw.get("choice")
    short = _shortfalls(ctx, ctx.location("pharmacy"))
    if choice == "substitute":
        item_id = raw.get("item_id")
        item = masters_repo.get_items(ctx.cur, ctx.hospital_id, [int(item_id or 0)]).get(
            int(item_id or 0)
        )
        if item is None or not item["active"]:
            raise invalid("Choose an active substitute item.", "substitute")
        for line in ctx.lines:
            if line["item_id"] in short:
                ctx.update_line(line, item_id=item["id"])
    elif choice == "emergency_purchase":
        vendor_id = raw.get("vendor_id")
        data = {"vendor_id": int(vendor_id)} if vendor_id else {}
        _open_linked(
            ctx,
            1,
            "f1_emergency",
            f"Emergency purchase for order #{ctx.case['id']}",
            data,
            [{"item_id": item_id, "qty": qty} for item_id, qty in short.items()],
        )
    else:
        raise invalid("Choose substitute or emergency purchase.", "substitute")
    ctx.data["substitution"] = choice


def ndps_entry(ctx: Ctx) -> None:
    """F3:"NDPS register entry, dual sign-off": one register row per controlled line, signed by
    two users (one user may sign twice only in demo mode, D-018)."""
    first = ctx.user.user_id
    second = (ctx.input.get("signoffs") or {}).get("signoff_2") or (
        first if ctx.demo_mode else None
    )
    if not second or (second == first and not ctx.demo_mode):
        raise AppError(
            403, "DUAL_SIGNOFF_REQUIRED", "A second, different user must sign the NDPS entry."
        )
    if second != first and not masters_repo.member_exists(ctx.cur, ctx.hospital_id, second):
        raise invalid("The second signatory is not a member of this hospital.", "signoffs")
    items = ctx.items()
    for line in ctx.lines:
        if items[line["item_id"]]["controlled_class"] != "none" and line_qty(line) > ZERO:
            cases_repo.insert_ndps(
                ctx.cur,
                ctx.hospital_id,
                ctx.case["id"],
                line["item_id"],
                line_qty(line),
                str(ctx.data.get("patient_ref") or ""),
                first,
                second,
            )


def dispense(ctx: Ctx) -> None:
    """F3:"Dispense from earliest-expiry batch": FEFO out of the pharmacy, recording the patient.
    Controlled items need their NDPS entry first. A shortfall (emergency purchase path) is first
    moved from the central store, as in FR-021a."""
    items = ctx.items()
    signed = cases_repo.ndps_item_ids(ctx.cur, ctx.hospital_id, ctx.case["id"])
    for line in ctx.lines:
        controlled = items[line["item_id"]]["controlled_class"] != "none"
        if controlled and line["item_id"] not in signed and line_qty(line) > ZERO:
            raise AppError(
                409, "NDPS_REQUIRED", "Record the NDPS register entry before dispensing."
            )
    pharmacy = ctx.location("pharmacy")
    short = _shortfalls(ctx, pharmacy)
    if short:
        _refill_from_central(ctx, short)
    ledger: list[dict] = []
    for line in ctx.lines:
        qty = line_qty(line)
        if qty == ZERO:
            continue
        moves, picks = stock.fefo_moves(
            ctx.cur, ctx.hospital_id, line["item_id"], pharmacy, None, qty, "in the pharmacy"
        )
        ledger += moves
        _record_issue(ctx, line, picks, pharmacy)
    ctx.post(
        "dispense",
        ledger,
        patient_ref=ctx.data.get("patient_ref"),
        prescriber=ctx.data.get("prescriber"),
        note=ctx.note,
    )


def _patient_returns(ctx: Ctx, target: int) -> list[dict]:
    return [
        {"item_id": line["item_id"], "batch_id": pick.batch_id, "location_id": target,
         "qty": pick.qty}
        for line in ctx.lines
        for pick in _returned_picks(ctx, line)
    ]


def patient_return_accept(ctx: Ctx) -> None:
    """F3:"Sealed, unexpired, cold chain OK?" Accepted: back into the pharmacy."""
    ctx.post(
        "return",
        _patient_returns(ctx, ctx.location("pharmacy")),
        patient_ref=ctx.data.get("patient_ref"),
        note=ctx.note,
    )


def patient_return_reject(ctx: Ctx) -> None:
    """Rejected: into expiry quarantine, and a linked Flow 4 case for disposal."""
    ledger = _patient_returns(ctx, ctx.location("quarantine_expiry"))
    ctx.post("return", ledger, patient_ref=ctx.data.get("patient_ref"), note=ctx.note)
    _quarantine_for_disposal(ctx, ledger, f"order #{ctx.case['id']}")


# ------------------------------------------------------------------------------------------------
# Flow 4 — Expiry management (FR-024)
# ------------------------------------------------------------------------------------------------


def _expiry_stock(ctx: Ctx) -> tuple[int, int, int, Decimal]:
    batch_id, location_id = int(ctx.data["batch_id"]), int(ctx.data["location_id"])
    item_id = ctx.lines[0]["item_id"] if ctx.lines else masters_repo.get_batch(
        ctx.cur, ctx.hospital_id, batch_id
    )["item_id"]
    qty = stock_repo.balance(ctx.cur, ctx.hospital_id, batch_id, location_id)
    return item_id, batch_id, location_id, qty


def _follow(ctx: Ctx, location_id: int, qty: Decimal | None = None) -> None:
    ctx.data["location_id"] = location_id
    for line in ctx.lines:
        changes: dict[str, Any] = {"location_id": location_id}
        if qty is not None:
            changes["qty_approved"] = qty
        ctx.update_line(line, **changes)


def expiry_move(ctx: Ctx) -> None:
    """F4:"Move to high-use ward or offer return to vendor": the user picks one (FR-024)."""
    raw = ctx.input.get("move_choice") or {}
    choice = raw.get("choice")
    if choice == "ward":
        ward = masters_repo.get_location(ctx.cur, ctx.hospital_id, int(raw.get("location_id") or 0))
        if ward is None or ward["kind"] not in WARD_KINDS:
            raise invalid("Choose the ward, OT or ICU to move the stock to.", "move_choice")
        item_id, batch_id, source, qty = _expiry_stock(ctx)
        ctx.post("transfer", stock.move(item_id, batch_id, source, ward["id"], qty), note=ctx.note)
        _follow(ctx, ward["id"])
    elif choice != "vendor":
        raise invalid("Choose move to a ward or offer return to vendor.", "move_choice")
    ctx.data["choice"] = choice


def expiry_vendor_return(ctx: Ctx) -> None:
    """F4:"Credit note received": the near-expiry stock leaves with the vendor."""
    item_id, batch_id, location_id, qty = _expiry_stock(ctx)
    ctx.post(
        "return_to_vendor",
        [{"item_id": item_id, "batch_id": batch_id, "location_id": location_id, "qty": -qty}],
        note=ctx.note,
    )


def expiry_quarantine(ctx: Ctx) -> None:
    """F4:"Used before expiry date?" No, expired: the remainder moves to expiry quarantine."""
    quarantine = ctx.location("quarantine_expiry")
    item_id, batch_id, source, qty = _expiry_stock(ctx)
    ctx.post("transfer", stock.move(item_id, batch_id, source, quarantine, qty), note=ctx.note)
    _follow(ctx, quarantine, qty)


def write_off(ctx: Ctx) -> None:
    """F4:"Dispose via biomedical waste vendor": the quarantined quantity is written off."""
    ledger: list[dict] = []
    for line in ctx.lines:
        held = stock_repo.balance(ctx.cur, ctx.hospital_id, line["batch_id"], line["location_id"])
        qty = min(line_qty(line), held)
        ledger.append(
            {"item_id": line["item_id"], "batch_id": line["batch_id"],
             "location_id": line["location_id"], "qty": -qty}
        )
    ctx.post("write_off", ledger, note=ctx.note)


# ------------------------------------------------------------------------------------------------
# Flow 5 — Recall (FR-025)
# ------------------------------------------------------------------------------------------------


def recall_find(ctx: Ctx) -> None:
    """F5:"Find affected batches in all stores and wards": snapshot of every holding."""
    holdings = stock_repo.batch_holdings(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"]))
    ctx.data["holdings"] = [
        {"location_id": h["location_id"], "location_name": h["location_name"],
         "qty": flt(h["qty"])}
        for h in holdings
    ]


def recall_block(ctx: Ctx) -> None:
    """F5:"Block and quarantine batches": never allocated again; all stock to recall quarantine."""
    batch_id = int(ctx.data["batch_id"])
    quarantine = ctx.location("quarantine_recall")
    masters_repo.block_batch(ctx.cur, ctx.hospital_id, batch_id)
    audit.append(
        ctx.cur,
        hospital_id=ctx.hospital_id,
        action="block",
        entity_type="batch",
        entity_id=str(batch_id),
        after={"blocked": True, "case_id": ctx.case["id"]},
        request_id=ctx.request_id,
    )
    ledger: list[dict] = []
    for h in stock_repo.batch_holdings(ctx.cur, ctx.hospital_id, batch_id):
        if h["location_id"] != quarantine:
            ledger += stock.move(h["item_id"], batch_id, h["location_id"], quarantine, h["qty"])
    ctx.post("transfer", ledger, note=ctx.note)


def recall_return(ctx: Ctx) -> None:
    """F5:"Return to vendor for credit or replacement": recall quarantine → vendor."""
    batch_id = int(ctx.data["batch_id"])
    quarantine = ctx.location("quarantine_recall")
    held = stock_repo.balance(ctx.cur, ctx.hospital_id, batch_id, quarantine)
    batch = masters_repo.get_batch(ctx.cur, ctx.hospital_id, batch_id)
    ctx.post(
        "return_to_vendor",
        [{"item_id": batch["item_id"], "batch_id": batch_id, "location_id": quarantine,
          "qty": -held}],
        note=ctx.note,
    )


EFFECTS: dict[str, Callable[[Ctx], None]] = {
    "approve_qty": approve_qty,
    "set_vendor": set_vendor,
    "grn_receive": grn_receive,
    "emergency_receive": emergency_receive,
    "qc_pass": qc_pass,
    "qc_fail": qc_fail,
    "raise_refill_requisition": raise_refill_requisition,
    "refill_pharmacy": refill_pharmacy,
    "issue_to_ward": issue_to_ward,
    "record_returns": record_returns,
    "ward_return_accept": ward_return_accept,
    "ward_return_reject": ward_return_reject,
    "modify_order": modify_order,
    "substitute": substitute,
    "ndps_entry": ndps_entry,
    "dispense": dispense,
    "patient_return_accept": patient_return_accept,
    "patient_return_reject": patient_return_reject,
    "expiry_move": expiry_move,
    "expiry_vendor_return": expiry_vendor_return,
    "expiry_quarantine": expiry_quarantine,
    "write_off": write_off,
    "recall_find": recall_find,
    "recall_block": recall_block,
    "recall_return": recall_return,
}
