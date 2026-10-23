"""Fictional demo data for every flow (FR-033, research R7). Run through
`python scripts/bootstrap.py demo-data`.

Masters and opening stock (D-017) are created first; then cases are raised and moved through
the same domain functions the API uses, acting as the demo user, so the seeded history, audit
entries and balances are exactly what real use would produce. Each stage is recorded in the
setting `demo.seed_stages_done`, so running again skips what is already there.

Patient references, prescribers, vendors and batch numbers are invented (D-018, constitution X).
"""

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from app.core.auth import CurrentUser
from app.core.dates import hospital_today
from app.domain import expiry, flows, stock
from app.repositories import masters

STAGES_KEY = "demo.seed_stages_done"
STAGES = (
    "masters", "opening_stock", "procurement", "ward_indent", "dispensing", "expiry", "recall",
)

LOCATIONS = [
    ("CS", "Central Store", "central_store"),
    ("PH", "Main Pharmacy", "pharmacy"),
    ("WM", "Medical Ward A", "ward"),
    ("WS", "Surgical Ward B", "ward"),
    ("OT", "Operation Theatre", "ot"),
    ("ICU", "ICU", "icu"),
    ("QQC", "QC Quarantine", "quarantine_qc"),
    ("QEX", "Expiry & Disposal Quarantine", "quarantine_expiry"),
    ("QRC", "Recall Quarantine", "quarantine_recall"),
]
VENDORS = [
    "Sunrise Pharma Distributors", "MedLine Healthcare", "Apex Surgicals", "Kaveri Drug House"
]
# name, base unit, formulary status, controlled class
ITEMS = [
    ("Paracetamol 500 mg tablet", "tablet", "formulary", "none"),
    ("Amoxicillin 500 mg capsule", "capsule", "formulary", "none"),
    ("Azithromycin 500 mg tablet", "tablet", "formulary", "none"),
    ("Ceftriaxone 1 g injection", "vial", "formulary", "none"),
    ("Metformin 500 mg tablet", "tablet", "formulary", "none"),
    ("Amlodipine 5 mg tablet", "tablet", "formulary", "none"),
    ("Atorvastatin 10 mg tablet", "tablet", "formulary", "none"),
    ("Pantoprazole 40 mg tablet", "tablet", "formulary", "none"),
    ("Ondansetron 4 mg injection", "ampoule", "formulary", "none"),
    ("Insulin regular 40 IU/ml", "vial", "formulary", "none"),
    ("Normal saline 0.9% 500 ml", "bottle", "formulary", "none"),
    ("Ringer lactate 500 ml", "bottle", "formulary", "none"),
    ("ORS sachet", "sachet", "formulary", "none"),
    ("Cetirizine 10 mg tablet", "tablet", "formulary", "none"),
    ("Salbutamol inhaler 100 mcg", "inhaler", "formulary", "none"),
    ("Heparin 5000 IU injection", "vial", "formulary", "none"),
    ("Enoxaparin 40 mg injection", "syringe", "formulary", "none"),
    ("Dexamethasone 4 mg injection", "ampoule", "formulary", "none"),
    ("Diclofenac 75 mg injection", "ampoule", "formulary", "none"),
    ("Ranitidine 150 mg tablet", "tablet", "formulary", "none"),
    ("Vitamin B complex tablet", "tablet", "formulary", "none"),
    ("Surgical gloves size 7", "pair", "formulary", "none"),
    ("IV cannula 20G", "piece", "formulary", "none"),
    ("Morphine 10 mg injection", "ampoule", "formulary", "narcotic"),
    ("Tramadol 50 mg capsule", "capsule", "formulary", "narcotic"),
    ("Alprazolam 0.5 mg tablet", "tablet", "formulary", "schedule_x"),
    ("Meropenem 1 g injection", "vial", "non_formulary", "none"),
    ("Linezolid 600 mg tablet", "tablet", "non_formulary", "none"),
    ("Brand-name multivitamin syrup", "bottle", "non_formulary", "none"),
    ("Imported hemostatic gauze", "piece", "non_formulary", "none"),
]
# Batches the flows' stories need: item, batch number, days to expiry, location, quantity.
SPECIAL_BATCHES = [
    ("Amoxicillin 500 mg capsule", "AMX-2024-17", 20, "PH", 120),
    ("ORS sachet", "ORS-0921", 50, "PH", 80),
    ("Insulin regular 40 IU/ml", "INS-7781", 80, "PH", 20),
    ("Cetirizine 10 mg tablet", "CTZ-3310", -5, "PH", 60),
    ("Pantoprazole 40 mg tablet", "PTZ-5502", 300, "CS", 200),
    ("Ondansetron 4 mg injection", "OND-1188", 150, "PH", 30),
]


class Seeder:
    """Acts as one user inside one open transaction; each step runs in a savepoint."""

    def __init__(self, conn: psycopg.Connection, user: CurrentUser) -> None:
        self.conn = conn
        self.user = user
        self.hid = user.hospital_id
        self.loc: dict[str, int] = {}
        self.item: dict[str, int] = {}
        self.vendor: dict[str, int] = {}
        self.today = date.today()

    def tx(self):
        @contextmanager
        def _tx():
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute("savepoint seed")
                yield cur
                cur.execute("release savepoint seed")

        return _tx()

    def load_masters(self) -> None:
        with self.tx() as cur:
            self.today = hospital_today(cur, self.hid)
            self.loc = {r["code"]: r["id"] for r in masters.list_locations(cur, self.hid)}
            self.vendor = {r["name"]: r["id"] for r in masters.list_vendors(cur, self.hid)}
            cur.execute("select id, name from public.items where hospital_id = %s", (self.hid,))
            self.item = {r["name"]: r["id"] for r in cur.fetchall()}

    # Building blocks -------------------------------------------------------------------------

    def raise_case(self, flow: int, start: str, title: str, data: dict, lines) -> int:
        body = {
            "flow": flow, "start": start, "title": title, "data": data,
            "lines": [{"item_id": self.item[name], "qty": qty} for name, qty in lines],
        }
        return flows.create_case(self.user, self.tx, body, "demo-data")["id"]

    def move(self, case_id: int, *steps) -> dict:
        """Each step is an edge id, or (edge id, input) / (edge id, input, note)."""
        for step in steps:
            edge, input_, note = (step, None, None) if isinstance(step, str) else (
                step + (None,) * (3 - len(step))
            )
            with self.tx() as cur:
                flows.move_case(cur, self.user, case_id, edge, note, input_, "demo-data", True)
        return flows.get_case(self.user, self.tx, case_id)

    def lines_of(self, case_id: int) -> list[dict]:
        return flows.get_case(self.user, self.tx, case_id)["lines"]

    def batches_input(self, case_id: int, prefix: str, days: int) -> dict:
        expiry_date = (self.today + timedelta(days=days)).isoformat()
        return {
            "batches": {
                str(ln["id"]): {"batch_no": f"{prefix}-{i + 1}", "expiry_date": expiry_date}
                for i, ln in enumerate(self.lines_of(case_id))
            }
        }

    def first_line(self, case_id: int) -> str:
        return str(self.lines_of(case_id)[0]["id"])

    # Stages ----------------------------------------------------------------------------------

    def masters(self) -> None:
        with self.tx() as cur:
            for code, name, kind in LOCATIONS:
                cur.execute(
                    "insert into public.locations (hospital_id, code, name, kind, allocatable)"
                    " values (%s, %s, %s, %s, %s) on conflict (hospital_id, code) do nothing",
                    (self.hid, code, name, kind, not kind.startswith("quarantine")),
                )
            for name in VENDORS:
                cur.execute(
                    "insert into public.vendors (hospital_id, name) values (%s, %s)"
                    " on conflict (hospital_id, name) do nothing",
                    (self.hid, name),
                )
            for name, unit, formulary, controlled in ITEMS:
                cur.execute(
                    "insert into public.items (hospital_id, name, base_unit, formulary_status,"
                    " controlled_class) select %s, %s, %s, %s, %s where not exists (select 1"
                    " from public.items where hospital_id = %s and lower(name) = lower(%s))",
                    (self.hid, name, unit, formulary, controlled, self.hid, name),
                )
        self.load_masters()

    def opening_stock(self) -> None:
        """D-017: stock on hand at go-live enters through one opening-stock document."""
        lines: list[dict] = []
        with self.tx() as cur:
            for n, (name, *_rest) in enumerate(ITEMS):
                for prefix, location, days, qty in (("CS", "CS", 400 + 7 * n, 500),
                                                    ("PH", "PH", 240 + 5 * n, 100)):
                    batch = masters.get_or_create_batch(
                        cur, self.hid, self.item[name], f"{prefix}{n:02d}-A",
                        self.today + timedelta(days=days), self.vendor[VENDORS[n % 4]],
                    )
                    lines.append({"item_id": self.item[name], "batch_id": batch["id"],
                                  "location_id": self.loc[location], "qty": Decimal(qty)})
            special: dict[str, int] = {}
            for n, (name, batch_no, days, location, qty) in enumerate(SPECIAL_BATCHES):
                batch = masters.get_or_create_batch(
                    cur, self.hid, self.item[name], batch_no, self.today + timedelta(days=days),
                    self.vendor[VENDORS[n % 4]],
                )
                special[batch_no] = batch["id"]
                lines.append({"item_id": self.item[name], "batch_id": batch["id"],
                              "location_id": self.loc[location], "qty": Decimal(qty)})
            stock.post_document(cur, self.hid, "opening", lines,
                                note="Opening stock at go-live (D-017)")
            # Some of the OND-1188 recall batch already sits on a ward.
            stock.post_document(
                cur, self.hid, "transfer",
                stock.move(self.item["Ondansetron 4 mg injection"], special["OND-1188"],
                           self.loc["PH"], self.loc["WM"], Decimal(6)),
                note="Ward stock at go-live",
            )

    def procurement(self) -> None:
        to_hod = ("f1_need__f1_raise", "f1_raise__f1_formulary", "f1_formulary__yes")
        po = ("f1_create_po__f1_finance", "f1_finance__approved",
              "f1_send_po__f1_vendor_response", "f1_vendor_response__confirmed",
              "f1_goods_arrive__f1_grn")
        sunrise, medline = self.vendor[VENDORS[0]], self.vendor[VENDORS[1]]

        case = self.raise_case(1, "f1_need", "Monthly reorder: analgesics",
                               {"trigger": "reorder_level"},
                               [("Paracetamol 500 mg tablet", 1000),
                                ("Diclofenac 75 mg injection", 200)])
        self.move(case, *to_hod, ("f1_hod__approved", {"approved_qty": {}}),
                  ("f1_rate_contract__yes", {"vendor_id": sunrise}), *po)
        self.move(case, ("f1_grn__all_ok", self.batches_input(case, "GRN-PCM", 500)),
                  "f1_qc_quarantine__f1_qc", "f1_qc__pass", "f1_stock_central__f1_invoice")

        case = self.raise_case(1, "f1_need", "ICU need: anticoagulants",
                               {"trigger": "department_need"},
                               [("Heparin 5000 IU injection", 100),
                                ("Enoxaparin 40 mg injection", 60)])
        self.move(case, *to_hod, ("f1_hod__approved", {"approved_qty": {}}),
                  "f1_rate_contract__no", "f1_rfq__f1_comparative",
                  ("f1_comparative__f1_create_po", {"vendor_id": medline}), *po)
        self.move(case, ("f1_grn__all_ok", self.batches_input(case, "GRN-ACG", 450)),
                  "f1_qc_quarantine__f1_qc", "f1_qc__pass", "f1_stock_central__f1_invoice")

        case = self.raise_case(1, "f1_need", "Non-formulary request: Meropenem",
                               {"trigger": "department_need"}, [("Meropenem 1 g injection", 40)])
        self.move(case, "f1_need__f1_raise", "f1_raise__f1_formulary", "f1_formulary__no",
                  "f1_committee_review__f1_committee",
                  ("f1_committee__rejected", None,
                   "Use Ceftriaxone 1 g injection from the formulary."),
                  "f1_reject__f1_closed")

        case = self.raise_case(1, "f1_need", "IV fluids top-up", {"trigger": "reorder_level"},
                               [("Normal saline 0.9% 500 ml", 300),
                                ("Ringer lactate 500 ml", 200)])
        self.move(case, *to_hod, ("f1_hod__approved", {"approved_qty": {}}),
                  ("f1_rate_contract__yes", {"vendor_id": sunrise}), *po)
        self.move(case, ("f1_grn__all_ok", self.batches_input(case, "GRN-IVF", 500)),
                  "f1_qc_quarantine__f1_qc")  # waits at F1:"QC passed?" (demo tour step 1)

        case = self.raise_case(1, "f1_need", "Gloves and cannulas",
                               {"trigger": "reorder_level"},
                               [("Surgical gloves size 7", 500), ("IV cannula 20G", 300)])
        self.move(case, *to_hod, ("f1_hod__approved", {"approved_qty": {}}),
                  ("f1_rate_contract__yes", {"vendor_id": self.vendor[VENDORS[2]]}),
                  "f1_create_po__f1_finance",
                  ("f1_finance__rejected", None, "Quantity above the quarterly budget."))

        case = self.raise_case(1, "f1_emergency", "Night emergency: Linezolid",
                               {"vendor_id": self.vendor[VENDORS[3]]},
                               [("Linezolid 600 mg tablet", 20)])
        self.move(case, "f1_emergency__f1_retro")

    def ward_indent(self) -> None:
        start = ("f2_need__f2_raise", "f2_raise__f2_review")

        case = self.raise_case(2, "f2_need", "Indent from Medical Ward A",
                               {"ward_location_id": self.loc["WM"]},
                               [("Paracetamol 500 mg tablet", 50),
                                ("Amoxicillin 500 mg capsule", 30)])
        self.move(case, *start, ("f2_review__approved_full_or_partial", {"approved_qty": {}}),
                  "f2_enough__yes", "f2_issue__f2_receipt", "f2_receipt__yes",
                  "f2_ward_stock__f2_unused")
        self.move(case, ("f2_unused__yes", {"return_qty": {self.first_line(case): 10}}),
                  "f2_return_slip__f2_check", "f2_check__accepted", "f2_restock__f2_closed")

        case = self.raise_case(2, "f2_need", "Indent from ICU",
                               {"ward_location_id": self.loc["ICU"]},
                               [("Heparin 5000 IU injection", 20),
                                ("Ceftriaxone 1 g injection", 15)])
        self.move(case, *start)  # waits at F2:"Pharmacist reviews indent" (demo tour step 2)

        case = self.raise_case(2, "f2_need", "Indent from Operation Theatre",
                               {"ward_location_id": self.loc["OT"]},
                               [("Surgical gloves size 7", 60), ("IV cannula 20G", 40)])
        lines = self.lines_of(case)
        self.move(case, *start,
                  ("f2_review__approved_full_or_partial",
                   {"approved_qty": {str(lines[0]["id"]): 30, str(lines[1]["id"]): 20}},
                   "Partial: OT has gloves in stock"),
                  "f2_enough__yes", "f2_issue__f2_receipt")

        case = self.raise_case(2, "f2_need", "Indent from Surgical Ward B",
                               {"ward_location_id": self.loc["WS"]},
                               [("Tramadol 50 mg capsule", 200)])
        self.move(case, *start, ("f2_review__rejected", None,
                                 "Narcotics are issued per patient order, not by ward indent."))

    def dispensing(self) -> None:
        def order(title, kind, patient, prescriber, lines):
            data = {"order_type": kind, "patient_ref": patient, "prescriber": prescriber}
            return self.raise_case(3, "f3_order", title, data, lines)

        verified = ("f3_order__f3_verify", "f3_verify__approved", "f3_in_stock__yes")

        case = order("OP order · OP-1042", "OP", "OP-1042", "Dr. Meera Nair",
                     [("Pantoprazole 40 mg tablet", 10), ("Amlodipine 5 mg tablet", 30)])
        self.move(case, *verified, "f3_controlled__no", "f3_dispense__f3_ip_op", "f3_ip_op__op",
                  "f3_op_bill__f3_counsel", "f3_counsel__f3_unused", "f3_unused__no")

        case = order("IP order · IP-2207 (post-op pain)", "IP", "IP-2207", "Dr. Arjun Rao",
                     [("Morphine 10 mg injection", 4)])
        self.move(case, *verified, "f3_controlled__yes",
                  ("f3_ndps__f3_dispense", {"signoffs": {}}), "f3_dispense__f3_ip_op",
                  "f3_ip_op__ip", "f3_ip_bill__f3_nurse")

        case = order("IP order · IP-2311", "IP", "IP-2311", "Dr. Kavya Iyer",
                     [("Ceftriaxone 1 g injection", 6)])
        self.move(case, *verified, "f3_controlled__no", "f3_dispense__f3_ip_op")

        case = order("OP order · OP-1077", "OP", "OP-1077", "Dr. Meera Nair",
                     [("Azithromycin 500 mg tablet", 5)])
        self.move(case, "f3_order__f3_verify",
                  ("f3_verify__rejected_or_unclear", None, "Duration not stated on the order."))

        order("OP order · OP-1090", "OP", "OP-1090", "Dr. Sameer Khan",
              [("Metformin 500 mg tablet", 60)])  # waits at the start (demo tour step 3)

    def expiry(self) -> None:
        created = expiry.scan(self.user, self.tx, "demo-data")["case_ids"]
        by_batch = {}
        for case_id in created:
            detail = flows.get_case(self.user, self.tx, case_id)
            by_batch[detail["lines"][0]["batch_no"]] = case_id
        to_move = ("f4_scan__f4_window", "f4_window__yes")
        if "CTZ-3310" in by_batch:  # expired Cetirizine: waits at write-off approval (step 4)
            self.move(by_batch["CTZ-3310"], *to_move,
                      ("f4_move__f4_vendor", {"move_choice": {"choice": "vendor"}}),
                      "f4_vendor__no", "f4_watch__f4_used", "f4_used__no_expired",
                      "f4_expired__f4_writeoff")
        if "ORS-0921" in by_batch:
            self.move(by_batch["ORS-0921"], *to_move,
                      ("f4_move__f4_vendor", {"move_choice": {"choice": "vendor"}}),
                      "f4_vendor__yes", ("f4_credit__f4_updated", None, "Credit note CN-2291"))
        if "AMX-2024-17" in by_batch:
            self.move(by_batch["AMX-2024-17"], *to_move,
                      ("f4_move__f4_vendor",
                       {"move_choice": {"choice": "ward", "location_id": self.loc["ICU"]}}),
                      "f4_vendor__no", "f4_watch__f4_used")

    def recall(self) -> None:
        with self.tx() as cur:
            batch_ids = {
                r["batch_no"]: r["id"]
                for r in masters.list_batches(cur, self.hid)
                if r["batch_no"] in ("OND-1188", "PTZ-5502", "INS-7781")
            }
        # A patient received OND-1188 before the alert (earliest expiry, so FEFO picks it).
        case = self.raise_case(3, "f3_order", "OP order · OP-1101",
                               {"order_type": "OP", "patient_ref": "OP-1101",
                                "prescriber": "Dr. Arjun Rao"},
                               [("Ondansetron 4 mg injection", 4)])
        self.move(case, "f3_order__f3_verify", "f3_verify__approved", "f3_in_stock__yes",
                  "f3_controlled__no", "f3_dispense__f3_ip_op")

        case = self.raise_case(5, "f5_notice", "Recall Ondansetron OND-1188 (CDSCO alert)",
                               {"batch_id": batch_ids["OND-1188"], "source": "regulator"}, [])
        self.move(case, "f5_notice__f5_find", "f5_find__f5_block",
                  "f5_block__f5_dispensed")  # waits at "Already dispensed?" (demo tour step 5)

        case = self.raise_case(5, "f5_notice", "Recall Pantoprazole PTZ-5502 (vendor notice)",
                               {"batch_id": batch_ids["PTZ-5502"], "source": "vendor"}, [])
        self.move(case, "f5_notice__f5_find")

        case = self.raise_case(5, "f5_notice", "Recall Insulin INS-7781 (vendor notice)",
                               {"batch_id": batch_ids["INS-7781"], "source": "vendor"}, [])
        self.move(case, "f5_notice__f5_find", "f5_find__f5_block", "f5_block__f5_dispensed",
                  "f5_dispensed__no", "f5_return__f5_updated")

    # Running ---------------------------------------------------------------------------------

    def run(self, log: Callable[[str], None] = print) -> list[str]:
        with self.tx() as cur:
            done = list(masters.get_setting(cur, self.hid, STAGES_KEY, []))
        self.load_masters()
        ran = []
        for stage in STAGES:
            if stage in done:
                continue
            getattr(self, stage)()
            done.append(stage)
            with self.tx() as cur:
                masters.set_setting(cur, self.hid, STAGES_KEY, done)
            ran.append(stage)
            log(f"demo data: {stage} done")
        if not ran:
            log("demo data: already loaded, nothing to do")
        return ran


def act_as(conn: psycopg.Connection, user: CurrentUser) -> None:
    conn.execute("set local role authenticated")
    conn.execute(
        "select set_config('request.jwt.claims', %s, true),"
        " set_config('request.jwt.claim.sub', %s, true)",
        (json.dumps(user.claims), user.user_id),
    )
