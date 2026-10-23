"""Flow engine, stock layer and flow effects against the database (US1 to US5, SC-003, SC-004).

Each test builds its own fictional hospital in one transaction that is rolled back, so nothing
is left behind. Needs DATABASE_URL with migrations applied; skipped otherwise.
"""

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.auth import CurrentUser
from app.core.config import get_settings
from app.core.errors import AppError
from app.domain import expiry, flows, inventory, stock

DATABASE_URL = get_settings().database_url
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")

PERMISSIONS = ("flows.read", "flows.write", "flows.approve", "stock.read", "masters.read",
               "items.read")
KINDS = {
    "CS": "central_store", "PH": "pharmacy", "WA": "ward", "ICU": "icu",
    "QQC": "quarantine_qc", "QEX": "quarantine_expiry", "QRC": "quarantine_recall",
}


@dataclass
class World:
    conn: psycopg.Connection
    hospital_id: int
    today: date
    users: dict[str, CurrentUser] = field(default_factory=dict)
    loc: dict[str, int] = field(default_factory=dict)
    items: dict[str, int] = field(default_factory=dict)
    vendor_id: int = 0
    current: str = ""

    def act_as(self, name: str) -> CurrentUser:
        user = self.users[name]
        self.conn.execute("set local role authenticated")
        self.conn.execute(
            "select set_config('request.jwt.claims', %s, true),"
            " set_config('request.jwt.claim.sub', %s, true)",
            (json.dumps(user.claims), user.user_id),
        )
        self.current = name
        return user

    @property
    def user(self) -> CurrentUser:
        return self.users[self.current]

    def tx(self):
        @contextmanager
        def _tx():
            with self.conn.cursor() as cur:
                cur.execute("savepoint step")
                try:
                    yield cur
                    cur.execute("release savepoint step")
                except Exception:
                    cur.execute("rollback to savepoint step")
                    raise

        return _tx()

    def batch(self, item: str, batch_no: str, days: int, blocked: bool = False) -> int:
        self.conn.execute("reset role")
        batch_id = self.conn.execute(
            "insert into public.batches (hospital_id, item_id, batch_no, expiry_date, blocked)"
            " values (%s, %s, %s, %s, %s) returning id",
            (self.hospital_id, self.items[item], batch_no, self.today + timedelta(days=days),
             blocked),
        ).fetchone()["id"]
        self.act_as(self.current or "store")
        return batch_id

    def stock_in(self, item: str, batch_id: int, location: str, qty: int) -> None:
        with self.tx() as cur:
            stock.post_document(
                cur, self.hospital_id, "opening",
                [{"item_id": self.items[item], "batch_id": batch_id,
                  "location_id": self.loc[location], "qty": Decimal(qty)}],
            )

    def balance(self, batch_id: int, location: str) -> Decimal:
        row = self.conn.execute(
            "select qty from public.stock_balances where hospital_id = %s and batch_id = %s"
            " and location_id = %s",
            (self.hospital_id, batch_id, self.loc[location]),
        ).fetchone()
        return row["qty"] if row else Decimal(0)

    def raise_case(self, flow: int, start: str, data: dict, lines: list[tuple[str, int]]):
        body = {
            "flow": flow, "start": start, "title": f"Test F{flow}", "data": data,
            "lines": [{"item_id": self.items[i], "qty": q} for i, q in lines],
        }
        return flows.create_case(self.user, self.tx, body, "test")

    def move(self, case: dict | int, edge: str, demo: bool = True, note=None, **input_):
        case_id = case if isinstance(case, int) else case["id"]
        body = {"edge": edge, "note": note, "input": input_ or None}
        return flows.take_action(self.user, self.tx, case_id, body, "test", demo)

    def walk(self, case, *edges: str):
        for edge in edges:
            case = self.move(case, edge)
        return case


def offered(case: dict) -> list[str]:
    return [a["id"] for a in case["actions"]]


@pytest.fixture
def w():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not conn.execute("select to_regclass('public.flow_cases') is not null as ok").fetchone()[
            "ok"
        ]:
            pytest.skip("migration 0002 not applied")
        try:
            yield build_world(conn)
        finally:
            conn.rollback()


def build_world(conn: psycopg.Connection) -> World:
    hospital_id = conn.execute(
        "insert into public.hospitals (name) values ('Flow test hospital') returning id"
    ).fetchone()["id"]
    today = conn.execute(
        "select (now() at time zone time_zone)::date as d from public.hospitals where id = %s",
        (hospital_id,),
    ).fetchone()["d"]
    world = World(conn, hospital_id, today)
    conn.execute(
        "insert into public.roles (hospital_id, code, name) values (%s, 'admin', 'Admin')",
        (hospital_id,),
    )
    for permission in PERMISSIONS:
        conn.execute(
            "insert into public.role_permissions values (%s, 'admin', %s)",
            (hospital_id, permission),
        )
    for name in ("store", "approver"):
        user_id = uuid.uuid4()
        conn.execute(
            "insert into auth.users (id, aud, role, email)"
            " values (%s, 'authenticated', 'authenticated', %s)",
            (user_id, f"{user_id}@example.test"),
        )
        conn.execute(
            "insert into public.hospital_members (hospital_id, user_id, display_name)"
            " values (%s, %s, %s)",
            (hospital_id, user_id, name.title()),
        )
        conn.execute(
            "insert into public.user_roles values (%s, %s, 'admin')", (hospital_id, user_id)
        )
        world.users[name] = CurrentUser(
            str(user_id), hospital_id, name.title(), frozenset({"admin"}),
            frozenset(PERMISSIONS), True, True, {"sub": str(user_id), "role": "authenticated"},
        )
    conn.execute(
        "insert into public.hospital_settings values (%s, 'expiry.alert_windows_days',"
        " '[90, 60, 30]'), (%s, 'allocation.block_on_expiry_date', 'true')",
        (hospital_id, hospital_id),
    )
    for code, kind in KINDS.items():
        world.loc[code] = conn.execute(
            "insert into public.locations (hospital_id, code, name, kind, allocatable)"
            " values (%s, %s, %s, %s, %s) returning id",
            (hospital_id, code, f"Test {code}", kind, not kind.startswith("quarantine")),
        ).fetchone()["id"]
    world.vendor_id = conn.execute(
        "insert into public.vendors (hospital_id, name) values (%s, 'Test Vendor') returning id",
        (hospital_id,),
    ).fetchone()["id"]
    for key, name, formulary, controlled in [
        ("para", "Test paracetamol", "formulary", "none"),
        ("morph", "Test morphine", "formulary", "narcotic"),
        ("mero", "Test meropenem", "non_formulary", "none"),
    ]:
        world.items[key] = conn.execute(
            "insert into public.items (hospital_id, name, base_unit, formulary_status,"
            " controlled_class, created_by) values (%s, %s, 'unit', %s, %s, %s) returning id",
            (hospital_id, name, formulary, controlled, world.users["store"].user_id),
        ).fetchone()["id"]
    world.act_as("store")
    return world


# Engine (FR-002 to FR-005) -----------------------------------------------------------------------


def test_only_outgoing_arrows_are_accepted(w):
    case = w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 5)])
    assert case["node"] == "f2_need" and offered(case) == ["f2_need__f2_raise"]
    with pytest.raises(AppError) as refused:
        w.move(case, "f2_review__rejected")
    assert refused.value.code == "INVALID_TRANSITION"


def test_reason_is_required(w):
    case = w.walk(
        w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 5)]),
        "f2_need__f2_raise", "f2_raise__f2_review",
    )
    with pytest.raises(AppError) as refused:
        w.move(case, "f2_review__rejected")
    assert refused.value.code == "VALIDATION_FAILED"
    case = w.move(case, "f2_review__rejected", note="Wrong ward")
    assert case["node"] == "f2_return_ward"
    assert w.move(case, "f2_return_ward__revise_and_resubmit")["node"] == "f2_raise"


def test_raiser_cannot_approve_outside_demo_mode(w):
    case = w.walk(
        w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 5)]),
        "f2_need__f2_raise", "f2_raise__f2_review",
    )
    with pytest.raises(AppError) as refused:
        w.move(case, "f2_review__rejected", demo=False, note="No")
    assert refused.value.code == "SELF_APPROVAL_FORBIDDEN"
    w.act_as("approver")
    case = w.move(case, "f2_review__rejected", demo=False, note="No")
    assert case["node"] == "f2_return_ward"


def test_closed_case_takes_no_action_and_history_is_recorded(w):
    case = w.raise_case(1, "f1_need", {"trigger": "reorder_level"}, [("mero", 5)])
    case = w.walk(case, "f1_need__f1_raise", "f1_raise__f1_formulary")
    # Non-formulary: only No is offered (R2).
    assert offered(case) == ["f1_formulary__no"]
    case = w.walk(case, "f1_formulary__no", "f1_committee_review__f1_committee")
    case = w.move(case, "f1_committee__rejected", note="Use the formulary alternative")
    case = w.move(case, "f1_reject__f1_closed")
    assert case["closed"] and case["actions"] == []
    assert [e["to_node"] for e in case["events"]][-2:] == ["f1_reject", "f1_closed"]
    with pytest.raises(AppError) as refused:
        w.move(case, "f1_reject__f1_closed")
    assert refused.value.code == "CASE_CLOSED"


# Stock layer (FR-012, FR-013, SC-003, SC-004) ----------------------------------------------------


def test_negative_balance_is_refused_and_nothing_is_saved(w):
    batch = w.batch("para", "NEG-1", 300)
    w.stock_in("para", batch, "PH", 5)
    before = w.conn.execute("select count(*) as n from public.stock_documents").fetchone()["n"]
    with pytest.raises(AppError) as refused, w.tx() as cur:
        stock.post_document(
            cur, w.hospital_id, "issue",
            stock.move(w.items["para"], batch, w.loc["PH"], w.loc["WA"], Decimal(6)),
        )
    assert refused.value.code == "INSUFFICIENT_STOCK"
    assert w.conn.execute("select count(*) as n from public.stock_documents").fetchone()["n"] == (
        before
    )
    assert w.balance(batch, "PH") == 5


def test_ledger_is_append_only(w):
    batch = w.batch("para", "AO-1", 300)
    w.stock_in("para", batch, "PH", 5)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), w.tx() as cur:
        cur.execute("update public.ledger_lines set qty = 1 where batch_id = %s", (batch,))


def test_fefo_order_excludes_expired_blocked_and_quarantine(w):
    late = w.batch("para", "LATE", 200)
    early = w.batch("para", "EARLY", 40)
    expired = w.batch("para", "GONE", 0)  # expires today: not allocatable (D-012)
    blocked = w.batch("para", "BLOCK", 10, blocked=True)
    for batch in (late, early, expired, blocked):
        w.stock_in("para", batch, "PH", 10)
    w.stock_in("para", w.batch("para", "QC", 5), "QQC", 10)
    with w.tx() as cur:
        picks, short = stock.allocate_fefo(cur, w.hospital_id, w.items["para"], w.loc["PH"],
                                           Decimal(15))
        quarantined, _ = stock.allocate_fefo(cur, w.hospital_id, w.items["para"], w.loc["QQC"],
                                             Decimal(1))
    assert picks == [stock.Pick(early, Decimal(10)), stock.Pick(late, Decimal(5))]
    assert short == 0 and quarantined == []


# Flow 1 — Procurement (US1) ----------------------------------------------------------------------


def test_requisition_to_central_store(w):
    case = w.raise_case(1, "f1_need", {"trigger": "reorder_level"}, [("para", 40)])
    case = w.walk(case, "f1_need__f1_raise", "f1_raise__f1_formulary")
    assert offered(case) == ["f1_formulary__yes"]
    case = w.move(case, "f1_formulary__yes")
    line = case["lines"][0]["id"]
    case = w.move(case, "f1_hod__approved", approved_qty={str(line): 30})
    case = w.move(case, "f1_rate_contract__yes", vendor_id=w.vendor_id)
    case = w.walk(case, "f1_create_po__f1_finance", "f1_finance__approved",
                  "f1_send_po__f1_vendor_response", "f1_vendor_response__confirmed",
                  "f1_goods_arrive__f1_grn")
    expiry_date = (w.today + timedelta(days=400)).isoformat()
    case = w.move(case, "f1_grn__all_ok",
                  batches={str(line): {"batch_no": "GRN-1", "expiry_date": expiry_date}})
    batch = case["lines"][0]["batch_id"]
    assert w.balance(batch, "QQC") == 30 and w.balance(batch, "CS") == 0
    case = w.walk(case, "f1_qc_quarantine__f1_qc", "f1_qc__pass")
    assert w.balance(batch, "QQC") == 0 and w.balance(batch, "CS") == 30
    case = w.move(case, "f1_stock_central__f1_invoice")
    assert case["closed"] and case["node"] == "f1_invoice"


def test_qc_fail_returns_to_vendor(w):
    case = w.raise_case(1, "f1_need", {"trigger": "reorder_level"}, [("para", 10)])
    case = w.walk(case, "f1_need__f1_raise", "f1_raise__f1_formulary", "f1_formulary__yes")
    case = w.move(case, "f1_hod__approved", approved_qty={})
    case = w.move(case, "f1_rate_contract__yes", vendor_id=w.vendor_id)
    case = w.walk(case, "f1_create_po__f1_finance", "f1_finance__approved",
                  "f1_send_po__f1_vendor_response", "f1_vendor_response__confirmed",
                  "f1_goods_arrive__f1_grn")
    line = case["lines"][0]["id"]
    case = w.move(case, "f1_grn__all_ok", batches={
        str(line): {"batch_no": "BAD-1", "expiry_date": (w.today + timedelta(days=99)).isoformat()}
    })
    case = w.walk(case, "f1_qc_quarantine__f1_qc", "f1_qc__fail")
    assert case["node"] == "f1_return_vendor"
    assert w.balance(case["lines"][0]["batch_id"], "QQC") == 0


def test_emergency_purchase_rejected_escalates(w):
    case = w.raise_case(1, "f1_emergency", {"vendor_id": w.vendor_id}, [("para", 5)])
    case = w.move(case, "f1_emergency__f1_retro")
    case = w.move(case, "f1_retro__rejected")
    assert case["closed"] and case["node"] == "f1_escalate"


# Flow 2 — Ward indent (US2) ----------------------------------------------------------------------


def test_indent_issues_earliest_expiry_first(w):
    early, late = w.batch("para", "E1", 60), w.batch("para", "L1", 300)
    w.stock_in("para", late, "PH", 50)
    w.stock_in("para", early, "PH", 10)
    case = w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 25)])
    case = w.walk(case, "f2_need__f2_raise", "f2_raise__f2_review")
    case = w.move(case, "f2_review__approved_full_or_partial", approved_qty={})
    assert offered(case) == ["f2_enough__yes"]
    case = w.walk(case, "f2_enough__yes", "f2_issue__f2_receipt")
    assert w.balance(early, "WA") == 10 and w.balance(late, "WA") == 15
    assert w.balance(early, "PH") == 0 and w.balance(late, "PH") == 35
    case = w.walk(case, "f2_receipt__yes", "f2_ward_stock__f2_unused")
    line = case["lines"][0]["id"]
    case = w.move(case, "f2_unused__yes", return_qty={str(line): 4})
    case = w.walk(case, "f2_return_slip__f2_check", "f2_check__accepted", "f2_restock__f2_closed")
    assert case["closed"]
    assert w.balance(early, "WA") == 6 and w.balance(early, "PH") == 4


def test_shortfall_opens_linked_requisition_and_refills_from_central(w):
    pharmacy, central = w.batch("para", "P1", 100), w.batch("para", "C1", 200)
    w.stock_in("para", pharmacy, "PH", 10)
    case = w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 30)])
    case = w.walk(case, "f2_need__f2_raise", "f2_raise__f2_review")
    case = w.move(case, "f2_review__approved_full_or_partial", approved_qty={})
    assert offered(case) == ["f2_enough__no"]
    case = w.move(case, "f2_enough__no")
    (child,) = case["child_case_ids"]
    linked = flows.get_case(w.user, w.tx, child)
    assert linked["flow"] == 1 and linked["parent_case_id"] == case["id"]
    assert linked["lines"][0]["qty_requested"] == Decimal(20)
    with pytest.raises(AppError) as refused:
        w.move(case, "f2_requisition__stock_received")
    assert refused.value.code == "INSUFFICIENT_STOCK"
    w.stock_in("para", central, "CS", 50)
    case = w.move(case, "f2_requisition__stock_received")
    assert w.balance(central, "CS") == 30 and w.balance(central, "PH") == 20
    case = w.move(case, "f2_issue__f2_receipt")
    assert w.balance(pharmacy, "WA") == 10 and w.balance(central, "WA") == 20


def test_rejected_ward_return_goes_to_flow_4(w):
    batch = w.batch("para", "R1", 300)
    w.stock_in("para", batch, "PH", 10)
    case = w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 10)])
    case = w.walk(case, "f2_need__f2_raise", "f2_raise__f2_review")
    case = w.move(case, "f2_review__approved_full_or_partial", approved_qty={})
    case = w.walk(case, "f2_enough__yes", "f2_issue__f2_receipt", "f2_receipt__no",
                  "f2_discrepancy__f2_ward_stock", "f2_ward_stock__f2_unused")
    case = w.move(case, "f2_unused__yes", return_qty={str(case["lines"][0]["id"]): 3})
    case = w.walk(case, "f2_return_slip__f2_check", "f2_check__rejected")
    assert w.balance(batch, "QEX") == 3 and w.balance(batch, "WA") == 7
    disposal = flows.get_case(w.user, w.tx, case["child_case_ids"][0])
    assert disposal["flow"] == 4 and disposal["node"] == "f4_expired"
    disposal = w.walk(disposal, "f4_expired__f4_writeoff", "f4_writeoff__approved",
                      "f4_dispose__f4_updated")
    assert disposal["closed"] and w.balance(batch, "QEX") == 0


# Flow 3 — Patient dispensing (US3) ---------------------------------------------------------------


def order(w, item: str, qty: int, order_type: str = "OP"):
    data = {"order_type": order_type, "patient_ref": "OP-9001", "prescriber": "Dr Test"}
    return w.raise_case(3, "f3_order", data, [(item, qty)])


def test_narcotic_needs_ndps_with_two_signoffs(w):
    batch = w.batch("morph", "M1", 300)
    w.stock_in("morph", batch, "PH", 20)
    case = w.walk(order(w, "morph", 4), "f3_order__f3_verify", "f3_verify__approved",
                  "f3_in_stock__yes")
    assert offered(case) == ["f3_controlled__yes"]
    case = w.move(case, "f3_controlled__yes")
    with pytest.raises(AppError) as refused:
        w.move(case, "f3_ndps__f3_dispense", demo=False, signoffs={})
    assert refused.value.code == "DUAL_SIGNOFF_REQUIRED"
    second = w.users["approver"].user_id
    case = w.move(case, "f3_ndps__f3_dispense", demo=False, signoffs={"signoff_2": second})
    case = w.move(case, "f3_dispense__f3_ip_op")
    assert w.balance(batch, "PH") == 16
    assert offered(case) == ["f3_ip_op__op"]
    case = w.walk(case, "f3_ip_op__op", "f3_op_bill__f3_counsel", "f3_counsel__f3_unused",
                  "f3_unused__no")
    assert case["closed"]
    register = w.conn.execute(
        "select signoff_1::text, signoff_2::text from public.ndps_register where case_id = %s",
        (case["id"],),
    ).fetchone()
    assert register == {"signoff_1": w.user.user_id, "signoff_2": second}


def test_query_loop_and_ip_path(w):
    batch = w.batch("para", "IP1", 300)
    w.stock_in("para", batch, "PH", 20)
    case = order(w, "para", 5, "IP")
    case = w.move(case, "f3_order__f3_verify")
    case = w.move(case, "f3_verify__rejected_or_unclear", note="Dose unclear")
    case = w.move(case, "f3_query__f3_response")
    line = case["lines"][0]["id"]
    case = w.move(case, "f3_response__order_modified", order_lines={str(line): 6})
    case = w.walk(case, "f3_verify__approved", "f3_in_stock__yes")
    assert offered(case) == ["f3_controlled__no"]
    case = w.walk(case, "f3_controlled__no", "f3_dispense__f3_ip_op")
    assert offered(case) == ["f3_ip_op__ip"] and w.balance(batch, "PH") == 14
    case = w.walk(case, "f3_ip_op__ip", "f3_ip_bill__f3_nurse", "f3_nurse__f3_unused")
    case = w.move(case, "f3_unused__yes", return_qty={str(line): 2})
    case = w.walk(case, "f3_check__accepted", "f3_restock__f3_closed")
    assert case["closed"] and w.balance(batch, "PH") == 16


def test_not_in_stock_opens_emergency_purchase(w):
    case = w.walk(order(w, "para", 5), "f3_order__f3_verify", "f3_verify__approved")
    assert offered(case) == ["f3_in_stock__no"]
    case = w.walk(case, "f3_in_stock__no")
    case = w.move(case, "f3_substitute__f3_controlled",
                  substitute={"choice": "emergency_purchase", "vendor_id": w.vendor_id})
    linked = flows.get_case(w.user, w.tx, case["child_case_ids"][0])
    assert linked["node"] == "f1_emergency" and linked["data"]["vendor_id"] == w.vendor_id


# Flow 4 — Expiry (US4) ---------------------------------------------------------------------------


def test_expiry_scan_windows_and_no_duplicates(w):
    for days in (20, 50, 80, 200):
        w.stock_in("para", w.batch("para", f"X{days}", days), "PH", 5)
    result = expiry.scan(w.user, w.tx, "test")
    windows = sorted(
        flows.get_case(w.user, w.tx, cid)["data"]["window_days"] for cid in result["case_ids"]
    )
    assert windows == [30, 60, 90]
    assert expiry.scan(w.user, w.tx, "test")["created"] == 0


def test_expiry_vendor_return_and_expired_write_off(w):
    soon = w.batch("para", "S20", 20)
    w.stock_in("para", soon, "PH", 8)
    (case_id,) = expiry.scan(w.user, w.tx, "test")["case_ids"]
    case = w.walk(case_id, "f4_scan__f4_window", "f4_window__yes")
    case = w.move(case, "f4_move__f4_vendor", move_choice={"choice": "vendor"})
    case = w.walk(case, "f4_vendor__yes", "f4_credit__f4_updated")
    assert case["closed"] and w.balance(soon, "PH") == 0

    other = w.batch("para", "S25", 25)
    w.stock_in("para", other, "PH", 6)
    (case_id,) = expiry.scan(w.user, w.tx, "test")["case_ids"]
    case = w.walk(case_id, "f4_scan__f4_window", "f4_window__yes")
    case = w.move(case, "f4_move__f4_vendor", move_choice={"choice": "ward",
                                                            "location_id": w.loc["ICU"]})
    assert offered(case) == ["f4_vendor__no"] and w.balance(other, "ICU") == 6
    case = w.walk(case, "f4_vendor__no", "f4_watch__f4_used", "f4_used__no_expired")
    assert w.balance(other, "QEX") == 6 and w.balance(other, "ICU") == 0
    case = w.move(case, "f4_expired__f4_writeoff")
    case = w.move(case, "f4_writeoff__rejected", note="Count again")
    case = w.walk(case, "f4_investigate__resubmit", "f4_writeoff__approved",
                  "f4_dispose__f4_updated")
    assert case["closed"] and w.balance(other, "QEX") == 0


# Flow 5 — Recall (US5) ---------------------------------------------------------------------------


def test_recall_quarantines_everywhere_and_traces_patients(w):
    batch = w.batch("para", "RC1", 300)
    for location, qty in (("CS", 20), ("PH", 10), ("WA", 5)):
        w.stock_in("para", batch, location, qty)
    case = w.walk(order(w, "para", 3), "f3_order__f3_verify", "f3_verify__approved",
                  "f3_in_stock__yes", "f3_controlled__no", "f3_dispense__f3_ip_op")
    recall = w.raise_case(5, "f5_notice", {"batch_id": batch, "source": "regulator"}, [])
    recall = w.move(recall, "f5_notice__f5_find")
    assert {h["location_id"] for h in recall["data"]["holdings"]} == {
        w.loc["CS"], w.loc["PH"], w.loc["WA"]
    }
    recall = w.walk(recall, "f5_find__f5_block", "f5_block__f5_dispensed")
    assert w.balance(batch, "QRC") == 32
    assert all(w.balance(batch, loc) == 0 for loc in ("CS", "PH", "WA"))
    assert offered(recall) == ["f5_dispensed__yes"]
    trace = inventory.batch_trace(w.user, w.tx, batch)
    assert trace["batch"]["blocked"]
    assert [(d["patient_ref"], d["qty"]) for d in trace["dispensed"]] == [("OP-9001", 3)]
    with w.tx() as cur:
        picks, _ = stock.allocate_fefo(cur, w.hospital_id, w.items["para"], w.loc["QRC"],
                                       Decimal(1))
    assert picks == []
    recall = w.walk(recall, "f5_dispensed__yes", "f5_inform__f5_return", "f5_return__f5_updated")
    assert recall["closed"] and w.balance(batch, "QRC") == 0
    assert case["node"] == "f3_ip_op"


def test_balances_equal_ledger_sums(w):
    batch = w.batch("para", "SUM1", 300)
    w.stock_in("para", batch, "PH", 12)
    case = w.raise_case(2, "f2_need", {"ward_location_id": w.loc["WA"]}, [("para", 7)])
    case = w.walk(case, "f2_need__f2_raise", "f2_raise__f2_review")
    case = w.move(case, "f2_review__approved_full_or_partial", approved_qty={})
    w.walk(case, "f2_enough__yes", "f2_issue__f2_receipt")
    mismatched = w.conn.execute(
        "select count(*) as n from public.stock_balances sb where sb.hospital_id = %s"
        " and sb.qty <> (select coalesce(sum(qty), 0) from public.ledger_lines l"
        "   where l.hospital_id = sb.hospital_id and l.batch_id = sb.batch_id"
        "   and l.location_id = sb.location_id)",
        (w.hospital_id,),
    ).fetchone()["n"]
    assert mismatched == 0
