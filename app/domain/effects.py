"""What happens when a case takes an edge: guards decide which data-driven outcomes are offered
(research R2); effects post stock and open linked cases (FR-020–FR-025). Everything runs in the
case move's transaction."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cached_property
from typing import Any

from psycopg import Cursor

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.dates import hospital_today
from app.core.errors import AppError
from app.domain import stock
from app.flows.definitions import Edge
from app.repositories import audit, masters
from app.repositories import cases as repo
from app.repositories import stock as stock_repo


@dataclass
class Ctx:
    cur: Cursor
    user: CurrentUser
    case: dict
    lines: list[dict]
    data: dict
    edge: Edge
    input: dict
    note: str | None
    request_id: str | None
    _locations: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def hospital_id(self) -> int:
        return self.user.hospital_id

    # Looked up once per move, not once per line: each lookup is a database round trip.
    @cached_property
    def today(self) -> date:
        return hospital_today(self.cur, self.hospital_id)

    @cached_property
    def block_on_expiry(self) -> bool:
        return bool(
            masters.get_setting(self.cur, self.hospital_id, "allocation.block_on_expiry_date", True)
        )

    def location(self, kind: str) -> int:
        if kind not in self._locations:
            row = masters.location_by_kind(self.cur, self.hospital_id, kind)
            if row is None:
                raise AppError(
                    409, "LOCATION_MISSING", f"No {kind.replace('_', ' ')} location set up."
                )
            self._locations[kind] = row["id"]
        return self._locations[kind]

    def allocate(self, item_id: int, location_id: int, qty: Decimal) -> stock.Allocation:
        return stock.allocate_fefo(
            self.cur,
            self.hospital_id,
            item_id,
            location_id,
            qty,
            today=self.today,
            block_on_expiry=self.block_on_expiry,
        )


def _invalid(message: str) -> AppError:
    return AppError(422, "VALIDATION_FAILED", message)


def _qty(value: Any, what: str) -> Decimal:
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise _invalid(f"{what} must be a number.") from exc
    if qty < 0:
        raise _invalid(f"{what} cannot be negative.")
    return qty


def line_qty(line: dict) -> Decimal:
    return Decimal(
        line["qty_approved"] if line["qty_approved"] is not None else line["qty_requested"]
    )


def _per_line(ctx: Ctx, key: str) -> dict:
    values = ctx.input.get(key) or {}
    if not isinstance(values, dict):
        raise _invalid(f"{key} must map line IDs to values.")
    # A blank field means "not entered", the same as leaving the line out.
    return {int(k): v for k, v in values.items() if v not in (None, "")}


def _post(ctx: Ctx, doc_type: str, lines: list[dict], **extra: Any) -> None:
    stock.post_document(
        ctx.cur,
        ctx.user,
        doc_type,
        lines,
        request_id=ctx.request_id,
        case_id=ctx.case["id"],
        note=ctx.note,
        **extra,
    )


def open_linked_case(
    ctx: Ctx, flow: int, node: str, title: str, data: dict, lines: list[dict]
) -> int:
    """Opens a case in another flow, linked to this one (FR-021, FR-021a, FR-022)."""
    child = repo.insert_case(ctx.cur, ctx.hospital_id, flow, node, title, data, ctx.case["id"])
    for line in lines:
        repo.insert_line(
            ctx.cur,
            ctx.hospital_id,
            child["id"],
            line["item_id"],
            line["qty"],
            line.get("batch_id"),
            line.get("location_id"),
        )
    repo.insert_event(
        ctx.cur,
        ctx.hospital_id,
        child["id"],
        None,
        None,
        node,
        f"Opened from case #{ctx.case['id']}",
        None,
    )
    audit.append(
        ctx.cur,
        hospital_id=ctx.hospital_id,
        action="create",
        entity_type="flow_case",
        entity_id=str(child["id"]),
        after={"flow": flow, "node": node, "parent_case_id": ctx.case["id"]},
        request_id=ctx.request_id,
    )
    ctx.data.setdefault("linked_case_ids", []).append(child["id"])
    return child["id"]


def _shortfalls(ctx: Ctx, location_id: int) -> dict:
    """Per line, how much the location cannot cover by FEFO allocation."""
    result = {}
    for line in ctx.lines:
        allocation = ctx.allocate(line["item_id"], location_id, line_qty(line))
        result[line["id"]] = allocation.shortfall
    return result


def _refill_from_central(ctx: Ctx, needed: dict[int, Decimal]) -> None:
    """Moves shortfalls from the central store to the pharmacy by FEFO (FR-021a)."""
    central, pharmacy = ctx.location("central_store"), ctx.location("pharmacy")
    moves = []
    for line in ctx.lines:
        qty = needed.get(line["id"], Decimal(0))
        if qty <= 0:
            continue
        allocation = ctx.allocate(line["item_id"], central, qty)
        if not allocation.covered:
            raise AppError(
                409,
                "INSUFFICIENT_STOCK",
                f"The central store cannot cover {line['item_name']} yet "
                f"(short by {allocation.shortfall}). Complete the linked requisition first.",
            )
        moves += stock.move_lines(line["item_id"], allocation.picks, central, pharmacy)
    if moves:
        _post(ctx, "transfer", moves)


def _issue_from_pharmacy(ctx: Ctx, doc_type: str, to_location: int | None, **extra: Any) -> None:
    """FEFO issue or dispense out of the pharmacy; records the batches used per line."""
    pharmacy = ctx.location("pharmacy")
    moves, issued = [], {}
    for line in ctx.lines:
        qty = line_qty(line)
        if qty <= 0:
            continue
        allocation = ctx.allocate(line["item_id"], pharmacy, qty)
        if not allocation.covered:
            raise AppError(
                409,
                "INSUFFICIENT_STOCK",
                f"The pharmacy cannot cover {line['item_name']} (short by {allocation.shortfall}).",
            )
        moves += stock.move_lines(line["item_id"], allocation.picks, pharmacy, to_location)
        issued[str(line["id"])] = [
            {"batch_id": p.batch_id, "qty": str(p.qty)} for p in allocation.picks
        ]
        repo.update_line(
            ctx.cur,
            ctx.hospital_id,
            line["id"],
            batch_id=allocation.picks[0].batch_id,
            location_id=to_location,
        )
    _post(ctx, doc_type, moves, **extra)
    ctx.data["issued"] = issued


def _return_picks(ctx: Ctx) -> list[tuple[dict, int, Decimal]]:
    """(line, batch_id, qty) to return, taken from the batches issued, latest-expiry first."""
    returns = ctx.data.get("returns") or {}
    result = []
    for line in ctx.lines:
        remaining = Decimal(returns.get(str(line["id"]), "0"))
        for pick in reversed(ctx.data.get("issued", {}).get(str(line["id"]), [])):
            if remaining <= 0:
                break
            take = min(remaining, Decimal(pick["qty"]))
            result.append((line, pick["batch_id"], take))
            remaining -= take
    return result


def _move_returns(ctx: Ctx, from_location: int | None, to_location: int, doc_type: str) -> None:
    lines = []
    for line, batch_id, qty in _return_picks(ctx):
        if from_location is not None:
            lines.append(
                {
                    "item_id": line["item_id"],
                    "batch_id": batch_id,
                    "location_id": from_location,
                    "qty": -qty,
                }
            )
        lines.append(
            {
                "item_id": line["item_id"],
                "batch_id": batch_id,
                "location_id": to_location,
                "qty": qty,
            }
        )
    _post(ctx, doc_type, lines, patient_ref=ctx.data.get("patient_ref"))


def _open_disposal_cases(ctx: Ctx, quarantine: int) -> None:
    """Rejected returns go on to Flow 4 at "Expired: quarantine" (clarified 2026-10-22)."""
    for line, batch_id, qty in _return_picks(ctx):
        open_linked_case(
            ctx,
            4,
            "f4_expired",
            f"Disposal of returned {line['item_name']} (case #{ctx.case['id']})",
            {"batch_id": batch_id, "location_id": quarantine},
            [
                {
                    "item_id": line["item_id"],
                    "qty": qty,
                    "batch_id": batch_id,
                    "location_id": quarantine,
                }
            ],
        )


def _batch_line(ctx: Ctx) -> dict:
    if not ctx.lines or ctx.lines[0]["batch_id"] is None:
        raise AppError(409, "CASE_INCOMPLETE", "This case has no batch.")
    return ctx.lines[0]


def _whole_batch(ctx: Ctx, location_id: int, to_location: int | None, doc_type: str) -> None:
    line = _batch_line(ctx)
    picks = stock.whole_batch_pick(ctx.cur, ctx.hospital_id, line["batch_id"], location_id)
    if not picks:
        raise AppError(409, "INSUFFICIENT_STOCK", "No stock of this batch left at the location.")
    _post(ctx, doc_type, stock.move_lines(line["item_id"], picks, location_id, to_location))


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


def approve_qty(ctx: Ctx) -> None:
    values = _per_line(ctx, "approved_qty")
    approved_any = False
    for line in ctx.lines:
        qty = _qty(values.get(line["id"], line["qty_requested"]), "Approved quantity")
        if qty > Decimal(line["qty_requested"]):
            raise _invalid("Approved quantity cannot exceed the requested quantity.")
        approved_any = approved_any or qty > 0
        repo.update_line(ctx.cur, ctx.hospital_id, line["id"], qty_approved=qty)
        line["qty_approved"] = qty
    if not approved_any:
        raise _invalid("Approve at least one quantity, or reject instead.")


def set_vendor(ctx: Ctx) -> None:
    vendor_id = ctx.input.get("vendor_id")
    if not vendor_id:
        raise _invalid("Select a vendor.")
    ctx.data["vendor_id"] = int(vendor_id)


def _receive(ctx: Ctx, kind: str) -> None:
    values = _per_line(ctx, "batches")
    target = ctx.location(kind)
    lines = []
    for line in ctx.lines:
        entry = values.get(line["id"]) or {}
        batch_no = str(entry.get("batch_no", "")).strip()
        if not batch_no or not entry.get("expiry_date"):
            raise _invalid(f"Enter batch number and expiry date for {line['item_name']}.")
        try:
            expiry = date.fromisoformat(str(entry["expiry_date"]))
        except ValueError as exc:
            raise _invalid("Expiry date must be YYYY-MM-DD.") from exc
        batch = masters.get_or_create_batch(
            ctx.cur, ctx.hospital_id, line["item_id"], batch_no, expiry, ctx.data.get("vendor_id")
        )
        if batch["expiry_date"] != expiry:
            raise _invalid(f"Batch {batch_no} already exists with a different expiry date.")
        repo.update_line(
            ctx.cur, ctx.hospital_id, line["id"], batch_id=batch["id"], location_id=target
        )
        line["batch_id"] = batch["id"]
        lines.append(
            {
                "item_id": line["item_id"],
                "batch_id": batch["id"],
                "location_id": target,
                "qty": line_qty(line),
            }
        )
    _post(ctx, "receipt", lines)


def grn_receive(ctx: Ctx) -> None:
    _receive(ctx, "quarantine_qc")


def emergency_receive(ctx: Ctx) -> None:
    _receive(ctx, "central_store")


def _from_qc(ctx: Ctx, to_kind: str | None, doc_type: str) -> None:
    qc = ctx.location("quarantine_qc")
    to_location = ctx.location(to_kind) if to_kind else None
    lines = []
    for line in ctx.lines:
        if line["batch_id"] is None:
            raise AppError(409, "CASE_INCOMPLETE", "Goods have not been received yet.")
        qty = line_qty(line)
        lines.append(
            {
                "item_id": line["item_id"],
                "batch_id": line["batch_id"],
                "location_id": qc,
                "qty": -qty,
            }
        )
        if to_location:
            lines.append(
                {
                    "item_id": line["item_id"],
                    "batch_id": line["batch_id"],
                    "location_id": to_location,
                    "qty": qty,
                }
            )
        repo.update_line(ctx.cur, ctx.hospital_id, line["id"], location_id=to_location)
    _post(ctx, doc_type, lines)


def qc_pass(ctx: Ctx) -> None:
    _from_qc(ctx, "central_store", "transfer")


def qc_fail(ctx: Ctx) -> None:
    _from_qc(ctx, None, "return_to_vendor")


def raise_refill_requisition(ctx: Ctx) -> None:
    short = _shortfalls(ctx, ctx.location("pharmacy"))
    lines = [
        {"item_id": line["item_id"], "qty": short[line["id"]]}
        for line in ctx.lines
        if short[line["id"]] > 0
    ]
    open_linked_case(
        ctx,
        1,
        "f1_need",
        f"Refill for indent #{ctx.case['id']}",
        {"trigger": "department_need"},
        lines,
    )


def refill_pharmacy(ctx: Ctx) -> None:
    _refill_from_central(ctx, _shortfalls(ctx, ctx.location("pharmacy")))


def issue_to_ward(ctx: Ctx) -> None:
    ward = ctx.data.get("ward_location_id")
    if not ward:
        raise AppError(409, "CASE_INCOMPLETE", "The indent has no ward.")
    _issue_from_pharmacy(ctx, "issue", int(ward))


def record_return_qty(ctx: Ctx) -> None:
    values = _per_line(ctx, "return_qty")
    returns = {}
    for line in ctx.lines:
        qty = _qty(values.get(line["id"], 0), "Return quantity")
        issued = sum(Decimal(p["qty"]) for p in ctx.data.get("issued", {}).get(str(line["id"]), []))
        if qty > issued:
            raise _invalid(f"Cannot return more {line['item_name']} than was issued ({issued}).")
        if qty > 0:
            returns[str(line["id"])] = str(qty)
    if not returns:
        raise _invalid("Enter the quantity being returned.")
    ctx.data["returns"] = returns


def ward_return_accept(ctx: Ctx) -> None:
    _move_returns(ctx, int(ctx.data["ward_location_id"]), ctx.location("pharmacy"), "return")


def ward_return_reject(ctx: Ctx) -> None:
    quarantine = ctx.location("quarantine_expiry")
    _move_returns(ctx, int(ctx.data["ward_location_id"]), quarantine, "return")
    _open_disposal_cases(ctx, quarantine)


def modify_order(ctx: Ctx) -> None:
    values = _per_line(ctx, "order_lines")
    for line in ctx.lines:
        if line["id"] in values:
            qty = _qty(values[line["id"]], "Quantity")
            repo.update_line(ctx.cur, ctx.hospital_id, line["id"], qty_requested=qty)


def substitute_or_purchase(ctx: Ctx) -> None:
    choice = (ctx.input.get("substitute") or {}).get("choice")
    pharmacy = ctx.location("pharmacy")
    short = _shortfalls(ctx, pharmacy)
    if choice == "substitute":
        item_id = (ctx.input.get("substitute") or {}).get("item_id")
        if not item_id:
            raise _invalid("Select the substitute item the prescriber agreed to.")
        for line in ctx.lines:
            if short[line["id"]] > 0:
                repo.update_line(ctx.cur, ctx.hospital_id, line["id"], item_id=int(item_id))
        ctx.data["substituted"] = True
    elif choice == "emergency_purchase":
        lines = [
            {"item_id": line["item_id"], "qty": short[line["id"]]}
            for line in ctx.lines
            if short[line["id"]] > 0
        ]
        open_linked_case(
            ctx,
            1,
            "f1_emergency",
            f"Emergency purchase for order #{ctx.case['id']}",
            {"vendor_id": (ctx.input.get("substitute") or {}).get("vendor_id")},
            lines,
        )
        ctx.data["emergency_purchase"] = True
    else:
        raise _invalid("Choose substitute or emergency purchase.")


def ndps_entry(ctx: Ctx) -> None:
    signoffs = ctx.input.get("signoffs") or {}
    first = str(ctx.user.user_id)
    second = str(signoffs.get("signoff_2") or first)
    if first == second and not get_settings().demo_mode:
        raise AppError(403, "SAME_SIGNATORY", "NDPS sign-off needs a second, different user.")
    for line in ctx.lines:
        if line["controlled_class"] != "none":
            repo.insert_ndps(
                ctx.cur,
                ctx.hospital_id,
                ctx.case["id"],
                line["item_id"],
                line_qty(line),
                ctx.data.get("patient_ref", ""),
                first,
                second,
            )


def dispense(ctx: Ctx) -> None:
    registered = {r["item_id"] for r in repo.ndps_entries(ctx.cur, ctx.hospital_id, ctx.case["id"])}
    for line in ctx.lines:
        if line["controlled_class"] != "none" and line["item_id"] not in registered:
            raise AppError(
                409, "NDPS_REQUIRED", f"{line['item_name']} needs an NDPS register entry first."
            )
    if ctx.data.get("emergency_purchase"):
        # Stock bought for this order arrives in the central store (Flow 1); bring it over.
        _refill_from_central(ctx, _shortfalls(ctx, ctx.location("pharmacy")))
    _issue_from_pharmacy(
        ctx,
        "dispense",
        None,
        patient_ref=ctx.data.get("patient_ref"),
        prescriber=ctx.data.get("prescriber"),
    )


def patient_return_accept(ctx: Ctx) -> None:
    _move_returns(ctx, None, ctx.location("pharmacy"), "return")


def patient_return_reject(ctx: Ctx) -> None:
    quarantine = ctx.location("quarantine_expiry")
    _move_returns(ctx, None, quarantine, "return")
    _open_disposal_cases(ctx, quarantine)


def move_or_offer(ctx: Ctx) -> None:
    choice = ctx.input.get("move_choice") or {}
    if choice.get("choice") == "ward":
        ward = choice.get("location_id")
        location = masters.get_location(ctx.cur, ctx.hospital_id, int(ward)) if ward else None
        if location is None or location["kind"] not in ("ward", "ot", "icu"):
            raise _invalid("Select the ward, OT or ICU to move the stock to.")
        _whole_batch(ctx, int(ctx.data["location_id"]), location["id"], "transfer")
        ctx.data["location_id"] = location["id"]
        repo.update_line(ctx.cur, ctx.hospital_id, ctx.lines[0]["id"], location_id=location["id"])
        ctx.data["choice"] = "ward"
    elif choice.get("choice") == "vendor":
        ctx.data["choice"] = "vendor"
    else:
        raise _invalid("Choose move to a ward or offer return to vendor.")


def expiry_return_to_vendor(ctx: Ctx) -> None:
    _whole_batch(ctx, int(ctx.data["location_id"]), None, "return_to_vendor")


def expiry_quarantine(ctx: Ctx) -> None:
    quarantine = ctx.location("quarantine_expiry")
    _whole_batch(ctx, int(ctx.data["location_id"]), quarantine, "transfer")
    ctx.data["location_id"] = quarantine
    repo.update_line(ctx.cur, ctx.hospital_id, ctx.lines[0]["id"], location_id=quarantine)


def dispose(ctx: Ctx) -> None:
    _whole_batch(ctx, int(ctx.data["location_id"]), None, "write_off")


def find_affected(ctx: Ctx) -> None:
    holdings = stock_repo.batch_holdings(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"]))
    ctx.data["holdings"] = [
        {"location_id": h["location_id"], "location_name": h["location_name"], "qty": str(h["qty"])}
        for h in holdings
    ]


def block_and_quarantine(ctx: Ctx) -> None:
    batch_id = int(ctx.data["batch_id"])
    recall = ctx.location("quarantine_recall")
    masters.block_batch(ctx.cur, ctx.hospital_id, batch_id)
    moves = []
    for holding in stock_repo.batch_holdings(ctx.cur, ctx.hospital_id, batch_id):
        if holding["location_id"] == recall:
            continue
        picks = stock.whole_batch_pick(ctx.cur, ctx.hospital_id, batch_id, holding["location_id"])
        moves += stock.move_lines(holding["item_id"], picks, holding["location_id"], recall)
    if moves:
        _post(ctx, "transfer", moves)


def recall_return_to_vendor(ctx: Ctx) -> None:
    batch_id = int(ctx.data["batch_id"])
    recall = ctx.location("quarantine_recall")
    picks = stock.whole_batch_pick(ctx.cur, ctx.hospital_id, batch_id, recall)
    if picks:
        batch = masters.get_batch(ctx.cur, ctx.hospital_id, batch_id)
        _post(ctx, "return_to_vendor", stock.move_lines(batch["item_id"], picks, recall, None))


EFFECTS: dict[str, Callable[[Ctx], None]] = {
    f.__name__: f
    for f in (
        approve_qty,
        set_vendor,
        grn_receive,
        emergency_receive,
        qc_pass,
        qc_fail,
        raise_refill_requisition,
        refill_pharmacy,
        issue_to_ward,
        record_return_qty,
        ward_return_accept,
        ward_return_reject,
        modify_order,
        substitute_or_purchase,
        ndps_entry,
        dispense,
        patient_return_accept,
        patient_return_reject,
        move_or_offer,
        expiry_return_to_vendor,
        expiry_quarantine,
        dispose,
        find_affected,
        block_and_quarantine,
        recall_return_to_vendor,
    )
}

# ---------------------------------------------------------------------------
# Guards (research R2): data decides which outcome of these decisions is offered
# ---------------------------------------------------------------------------


def _all_formulary(ctx: Ctx) -> bool:
    return all(line["formulary_status"] == "formulary" for line in ctx.lines)


def _pharmacy_covers(ctx: Ctx) -> bool:
    short = _shortfalls(ctx, ctx.location("pharmacy"))
    return all(v == 0 for v in short.values())


def _any_controlled(ctx: Ctx) -> bool:
    return any(line["controlled_class"] != "none" for line in ctx.lines)


def _days_left(ctx: Ctx) -> int:
    batch = masters.get_batch(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"]))
    return (batch["expiry_date"] - ctx.today).days


def _max_window(ctx: Ctx) -> int:
    windows = masters.get_setting(
        ctx.cur, ctx.hospital_id, "expiry.alert_windows_days", [90, 60, 30]
    )
    return max(int(w) for w in windows)


def _stock_left(ctx: Ctx) -> Decimal:
    return stock_repo.balance(
        ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"]), int(ctx.data["location_id"])
    )


def _expired(ctx: Ctx) -> bool:
    block_on_expiry = ctx.block_on_expiry
    days = _days_left(ctx)
    return days <= 0 if block_on_expiry else days < 0


def _dispensed(ctx: Ctx) -> bool:
    return bool(stock_repo.batch_dispensings(ctx.cur, ctx.hospital_id, int(ctx.data["batch_id"])))


GUARDS: dict[str, Callable[[Ctx], bool]] = {
    "formulary": _all_formulary,
    "non_formulary": lambda ctx: not _all_formulary(ctx),
    "pharmacy_covers": _pharmacy_covers,
    "pharmacy_short": lambda ctx: not _pharmacy_covers(ctx),
    "controlled": _any_controlled,
    "not_controlled": lambda ctx: not _any_controlled(ctx),
    "order_ip": lambda ctx: ctx.data.get("order_type") == "IP",
    "order_op": lambda ctx: ctx.data.get("order_type") == "OP",
    "within_window": lambda ctx: _days_left(ctx) <= _max_window(ctx),
    "outside_window": lambda ctx: _days_left(ctx) > _max_window(ctx),
    "offered_to_vendor": lambda ctx: ctx.data.get("choice") == "vendor",
    "stock_used": lambda ctx: _stock_left(ctx) == 0,
    "stock_left": lambda ctx: _stock_left(ctx) > 0 and _expired(ctx),
    "dispensed": _dispensed,
    "not_dispensed": lambda ctx: not _dispensed(ctx),
}
