"""Fictional demo data for the college demonstration (D-018, FR-033).

Creates locations, vendors, items, batches and opening stock (D-017), then raises cases in all
five flows and moves them through the same domain services the API uses, acting as the demo
user, so history, audit entries and balances are exactly what real use would produce.
Everything is fictional, including patient references (constitution X). Safe to run twice: it
does nothing if the hospital already has locations.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from app.core.auth import CurrentUser
from app.domain import expiry, flows, stock
from app.flows.definitions import FLOWS

LOCATIONS = [
    ("CS", "Central Store", "central_store", True),
    ("PH", "Main Pharmacy", "pharmacy", True),
    ("WM", "Medical Ward A", "ward", True),
    ("WS", "Surgical Ward B", "ward", True),
    ("OT", "Operation Theatre", "ot", True),
    ("ICU", "ICU", "icu", True),
    ("QQC", "QC Quarantine", "quarantine_qc", False),
    ("QEX", "Expiry & Disposal Quarantine", "quarantine_expiry", False),
    ("QRC", "Recall Quarantine", "quarantine_recall", False),
]
VENDORS = [
    "Sunrise Pharma Distributors",
    "MedLine Healthcare",
    "Apex Surgicals",
    "Kaveri Drug House",
]
# name, base unit, formulary, controlled class
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
    ("Imported hemostatic gauze", "pack", "non_formulary", "none"),
]


NOTABLE = {
    "amox_20": "AMX-2024-17",
    "ors_50": "ORS-0921",
    "insulin_80": "INS-7781",
    "cetirizine_expired": "CTZ-3310",
    "panto_recall": "PTZ-5502",
    "ondan_recall": "OND-1188",
}
STAGES = (
    "masters",
    "opening_stock",
    "procurement",
    "ward_indent",
    "dispensing",
    "expiry",
    "recall",
)
PROGRESS_KEY = "demo.seed_stages_done"


class Seeder:
    def __init__(self, conn: psycopg.Connection, user_id: str, hospital_id: int):
        self.conn = conn
        self.cur = conn.cursor(row_factory=dict_row)
        self.user_id = user_id
        self.hospital_id = hospital_id
        self.loc: dict[str, int] = {}
        self.items: dict[str, int] = {}
        self.vendors: list[int] = []
        self.today = date.today()

    # --- setup as the database owner ------------------------------------------------------

    def masters(self) -> None:
        for code, name, kind, allocatable in LOCATIONS:
            self.loc[code] = self.cur.execute(
                "insert into public.locations (hospital_id, code, name, kind, allocatable)"
                " values (%s, %s, %s, %s, %s) returning id",
                (self.hospital_id, code, name, kind, allocatable),
            ).fetchone()["id"]
        for name in VENDORS:
            self.vendors.append(
                self.cur.execute(
                    "insert into public.vendors (hospital_id, name) values (%s, %s) returning id",
                    (self.hospital_id, name),
                ).fetchone()["id"]
            )
        for name, unit, formulary, controlled in ITEMS:
            row = self.cur.execute(
                "insert into public.items (hospital_id, name, base_unit, formulary_status,"
                " controlled_class, created_by) values (%s, %s, %s, %s, %s, %s)"
                " on conflict (hospital_id, lower(name)) do update set name = excluded.name"
                " returning id",
                (self.hospital_id, name, unit, formulary, controlled, self.user_id),
            ).fetchone()
            self.items[name.split(" ")[0].lower()] = row["id"]
        for key, value in (
            ("expiry.alert_windows_days", [90, 60, 30]),
            ("allocation.block_on_expiry_date", True),
        ):
            self.cur.execute(
                "insert into public.hospital_settings (hospital_id, key, value)"
                " values (%s, %s, %s::jsonb) on conflict do nothing",
                (self.hospital_id, key, json.dumps(value)),
            )

    def reload_masters(self) -> None:
        """IDs of masters created by an earlier, committed stage (resuming a load)."""
        cur = self.cur
        self.loc = {
            r["code"]: r["id"]
            for r in cur.execute(
                "select code, id from public.locations where hospital_id = %s", (self.hospital_id,)
            ).fetchall()
        }
        self.vendors = [
            r["id"]
            for r in cur.execute(
                "select id from public.vendors where hospital_id = %s and name = any(%s)"
                " order by array_position(%s, name)",
                (self.hospital_id, VENDORS, VENDORS),
            ).fetchall()
        ]
        names = [name for name, *_ in ITEMS]
        self.items = {
            r["name"].split(" ")[0].lower(): r["id"]
            for r in cur.execute(
                "select id, name from public.items where hospital_id = %s and name = any(%s)",
                (self.hospital_id, names),
            ).fetchall()
        }

    def notable(self) -> dict[str, int]:
        """Batches the flows refer to, by batch number."""
        rows = self.cur.execute(
            "select batch_no, id from public.batches where hospital_id = %s and batch_no = any(%s)",
            (self.hospital_id, list(NOTABLE.values())),
        ).fetchall()
        by_no = {r["batch_no"]: r["id"] for r in rows}
        return {key: by_no[no] for key, no in NOTABLE.items()}

    def act_as_user(self) -> CurrentUser:
        claims = {"sub": self.user_id, "role": "authenticated"}
        self.cur.execute("set local role authenticated")
        self.cur.execute(
            "select set_config('request.jwt.claims', %s, true),"
            " set_config('request.jwt.claim.sub', %s, true)",
            (json.dumps(claims), self.user_id),
        )
        permissions = {
            r["permission_code"]
            for r in self.cur.execute(
                "select rp.permission_code from public.user_roles ur join public.role_permissions"
                " rp on rp.hospital_id = ur.hospital_id and rp.role_code = ur.role_code"
                " where ur.hospital_id = %s and ur.user_id = auth.uid()",
                (self.hospital_id,),
            ).fetchall()
        }
        self.user = CurrentUser(
            user_id=self.user_id,
            hospital_id=self.hospital_id,
            display_name="Demo",
            roles=frozenset({"admin"}),
            permissions=frozenset(permissions),
            privileged=True,
            mfa_satisfied=True,
            claims=claims,
        )
        return self.user

    # --- stock and cases as the demo user ---------------------------------------------------

    def batch(self, item: str, batch_no: str, days: int, vendor: int = 0) -> int:
        return self.cur.execute(
            "insert into public.batches (hospital_id, item_id, batch_no, expiry_date, vendor_id)"
            " values (%s, %s, %s, %s, %s) returning id",
            (
                self.hospital_id,
                self.items[item],
                batch_no,
                self.today + timedelta(days=days),
                self.vendors[vendor],
            ),
        ).fetchone()["id"]

    def opening_stock(self) -> dict[str, int]:
        """One opening-stock document per location (D-017). Returns notable batch IDs."""
        notable = {}
        central, pharmacy = [], []
        for n, (name, *_rest) in enumerate(ITEMS):
            key = name.split(" ")[0].lower()
            central.append((key, self.batch(key, f"CS{n:02d}-A", 400 + n * 7, n % 4), 200))
            pharmacy.append((key, self.batch(key, f"PH{n:02d}-A", 240 + n * 5, n % 4), 60))
        # Near-expiry and expired stock for Flow 4, and a recall candidate for Flow 5.
        notable["amox_20"] = self.batch("amoxicillin", "AMX-2024-17", 20, 1)
        notable["ors_50"] = self.batch("ors", "ORS-0921", 50, 2)
        notable["insulin_80"] = self.batch("insulin", "INS-7781", 80, 0)
        notable["cetirizine_expired"] = self.batch("cetirizine", "CTZ-3310", -4, 3)
        notable["panto_recall"] = self.batch("pantoprazole", "PTZ-5502", 300, 1)
        notable["ondan_recall"] = self.batch("ondansetron", "OND-1188", 150, 2)
        pharmacy += [
            ("amoxicillin", notable["amox_20"], 40),
            ("ors", notable["ors_50"], 80),
            ("insulin", notable["insulin_80"], 12),
            ("cetirizine", notable["cetirizine_expired"], 25),
            ("pantoprazole", notable["panto_recall"], 50),
            ("ondansetron", notable["ondan_recall"], 30),
        ]
        for code, rows in (("CS", central), ("PH", pharmacy)):
            stock.post_document(
                self.cur,
                self.user,
                "opening",
                [
                    {
                        "item_id": self.items[k],
                        "batch_id": b,
                        "location_id": self.loc[code],
                        "qty": Decimal(q),
                    }
                    for k, b, q in rows
                ],
                request_id=None,
                note="Opening stock at go-live (D-017)",
            )
        # Some pantoprazole PTZ-5502 already sits in a ward, for the recall trace.
        stock.post_document(
            self.cur,
            self.user,
            "transfer",
            [
                {
                    "item_id": self.items["pantoprazole"],
                    "batch_id": notable["panto_recall"],
                    "location_id": self.loc["PH"],
                    "qty": Decimal(-10),
                },
                {
                    "item_id": self.items["pantoprazole"],
                    "batch_id": notable["panto_recall"],
                    "location_id": self.loc["WM"],
                    "qty": Decimal(10),
                },
            ],
            request_id=None,
            note="Ward stock at go-live",
        )
        return notable

    def case(self, flow: int, start: str, title: str, data: dict, lines: list) -> int:
        rows = [
            {"item_id": self.items[k], "qty": q, **(extra[0] if extra else {})}
            for k, q, *extra in lines
        ]
        return flows.open_case(self.cur, self.user, flow, start, title, data, rows, None)["id"]

    def act(self, case_id: int, outcome: str | None = None, note: str | None = None, **input_):
        case = flows.repo.get_case(self.cur, self.hospital_id, case_id)
        edge = next(e for e in FLOWS[case["flow"]].outgoing(case["node"]) if e.outcome == outcome)
        flows.move(self.cur, self.user, case_id, edge.id, note, input_ or None, None)

    def walk(self, case_id: int, *steps) -> None:
        """Takes arrows in order. A step is an outcome (None for an unlabelled arrow) or
        (outcome, note) or (outcome, note, input)."""
        for step in steps:
            if isinstance(step, tuple):
                outcome, note, *rest = step
                self.act(case_id, outcome, note, **(rest[0] if rest else {}))
            else:
                self.act(case_id, step)

    def lines(self, case_id: int) -> list[int]:
        return [r["id"] for r in flows.repo.get_lines(self.cur, self.hospital_id, case_id)]

    def batches_input(self, case_id: int, prefix: str, days: int = 500) -> dict:
        expiry_date = (self.today + timedelta(days=days)).isoformat()
        return {
            str(i): {"batch_no": f"{prefix}-{n}", "expiry_date": expiry_date}
            for n, i in enumerate(self.lines(case_id), start=1)
        }

    # --- Flow 1 ---------------------------------------------------------------------------

    def procurement(self) -> None:
        v = self.vendors
        a = self.case(
            1,
            "f1_need",
            "Monthly reorder: analgesics",
            {"trigger": "reorder_level"},
            [("paracetamol", 500), ("diclofenac", 100)],
        )
        self.walk(
            a,
            None,
            None,
            "Yes",
            "Approved",
            ("Yes", None, {"vendor_id": v[0]}),
            None,
            ("Approved", "Within budget"),
            None,
            "Confirmed",
            None,
        )
        self.walk(
            a,
            ("All OK", None, {"batches": self.batches_input(a, "GRN-PCM")}),
            None,
            "Pass",
            (None, "Invoice INV-4471 matched"),
        )

        b = self.case(
            1,
            "f1_need",
            "ICU need: anticoagulants",
            {"trigger": "department_need"},
            [("heparin", 40), ("enoxaparin", 60)],
        )
        self.walk(b, None, None, "Yes")  # waiting at HOD approval

        c = self.case(
            1,
            "f1_need",
            "Non-formulary request: Meropenem",
            {"trigger": "department_need"},
            [("meropenem", 20)],
        )
        self.walk(
            c,
            None,
            None,
            "No",
            None,
            ("Rejected", "Use Ceftriaxone (formulary) unless culture shows resistance"),
            None,
        )

        d = self.case(
            1,
            "f1_need",
            "IV fluids top-up",
            {"trigger": "reorder_level"},
            [("normal", 300), ("ringer", 200)],
        )
        self.walk(
            d,
            None,
            None,
            "Yes",
            "Approved",
            "No",
            None,
            (None, None, {"vendor_id": v[1]}),
            None,
            "Approved",
            None,
            "Confirmed",
            None,
        )
        self.walk(d, ("All OK", None, {"batches": self.batches_input(d, "GRN-IVF")}), None)
        # waiting at QC passed?

        e = self.case(
            1,
            "f1_need",
            "Gloves and cannulas",
            {"trigger": "reorder_level"},
            [("surgical", 400), ("iv", 300)],
        )
        self.walk(
            e,
            None,
            None,
            "Yes",
            "Approved",
            ("Yes", None, {"vendor_id": v[2]}),
            None,
            ("Rejected", "Split into two POs"),
        )  # at Revise PO

        f = self.case(
            1,
            "f1_emergency",
            "Night emergency: Linezolid",
            {"vendor_id": v[3]},
            [("linezolid", 10)],
        )
        self.walk(f, None)  # waiting at Retrospective approval?

    # --- Flow 2 ---------------------------------------------------------------------------

    def ward_indent(self) -> None:
        a = self.case(
            2,
            "f2_need",
            "Indent from Medical Ward A",
            {"ward_location_id": self.loc["WM"]},
            [("paracetamol", 50), ("amoxicillin", 30)],
        )
        self.walk(a, None, None, "Approved (full or partial)", "Yes", None, "Yes", None)
        self.walk(
            a, ("Yes", None, {"return_qty": {str(self.lines(a)[0]): 10}}), None, "Accepted", None
        )

        b = self.case(
            2,
            "f2_need",
            "Indent from ICU",
            {"ward_location_id": self.loc["ICU"]},
            [("heparin", 10), ("normal", 20)],
        )
        self.walk(b, None, None)  # waiting for pharmacist review

        c = self.case(
            2,
            "f2_need",
            "Indent from Operation Theatre",
            {"ward_location_id": self.loc["OT"]},
            [("surgical", 40), ("iv", 20)],
        )
        first, second = self.lines(c)
        self.walk(
            c,
            None,
            None,
            (
                "Approved (full or partial)",
                "Partial: OT has gloves in stock",
                {"approved_qty": {str(first): 30, str(second): 20}},
            ),
            "Yes",
            None,
        )  # waiting at ward receipt

        d = self.case(
            2,
            "f2_need",
            "Indent from Surgical Ward B",
            {"ward_location_id": self.loc["WS"]},
            [("ceftriaxone", 200)],
        )
        self.walk(
            d, None, None, ("Rejected", "200 vials exceeds the ward's weekly use; revise to 40")
        )

    # --- Flow 3 ---------------------------------------------------------------------------

    def dispensing(self) -> None:
        a = self.case(
            3,
            "f3_order",
            "OP order · OP-1042",
            {"order_type": "OP", "patient_ref": "OP-1042", "prescriber": "Dr. Meera Nair"},
            [("pantoprazole", 10), ("amlodipine", 30)],
        )
        self.walk(a, None, "Approved", "Yes", "No", None, "OP", None, None, "No")

        b = self.case(
            3,
            "f3_order",
            "IP order · IP-2207 (post-op pain)",
            {"order_type": "IP", "patient_ref": "IP-2207", "prescriber": "Dr. Arjun Rao"},
            [("morphine", 4)],
        )
        self.walk(
            b,
            None,
            "Approved",
            "Yes",
            "Yes",
            (None, "Second sign-off: charge nurse (demo mode)"),
            None,
            "IP",
            None,
        )
        # waiting at Nurse administers

        c = self.case(
            3,
            "f3_order",
            "IP order · IP-2311",
            {"order_type": "IP", "patient_ref": "IP-2311", "prescriber": "Dr. Kavya Iyer"},
            [("ondansetron", 6), ("ceftriaxone", 6)],
        )
        self.walk(c, None, "Approved", "Yes", "No", None)
        # Ondansetron OND-1188 went to IP-2311 by FEFO: the recall case traces it.

        d = self.case(
            3,
            "f3_order",
            "OP order · OP-1077",
            {"order_type": "OP", "patient_ref": "OP-1077", "prescriber": "Dr. Sameer Khan"},
            [("azithromycin", 5)],
        )
        self.walk(d, None, ("Rejected or unclear", "Duration not written"))

        self.case(
            3,
            "f3_order",
            "OP order · OP-1090",
            {"order_type": "OP", "patient_ref": "OP-1090", "prescriber": "Dr. Meera Nair"},
            [("metformin", 60), ("atorvastatin", 30)],
        )

    # --- Flow 4 and 5 ---------------------------------------------------------------------

    def expiry(self, notable: dict) -> None:
        expiry.scan_cursor(self.cur, self.user, None)
        by_batch = {
            r["batch"]: r["id"]
            for r in self.cur.execute(
                "select id, (data->>'batch_id')::bigint as batch from public.flow_cases"
                " where hospital_id = %s and flow = 4 and (data->>'location_id')::bigint = %s",
                (self.hospital_id, self.loc["PH"]),
            ).fetchall()
        }
        vendor = {"move_choice": {"choice": "vendor"}}
        self.walk(
            by_batch[notable["ors_50"]],
            None,
            "Yes",
            (None, None, vendor),
            ("Yes", "Credit note CN-2291"),
            None,
        )
        self.walk(
            by_batch[notable["cetirizine_expired"]],
            None,
            "Yes",
            (None, None, vendor),
            ("No", "Vendor declined: past return window"),
            None,
            "No, expired",
            None,
        )
        # waiting at Write-off approved?
        ward = {"move_choice": {"choice": "ward", "location_id": self.loc["WM"]}}
        self.walk(by_batch[notable["amox_20"]], None, "Yes", (None, None, ward), "No", None)
        # on the watch list

    def recall(self, notable: dict) -> None:
        a = self.case(
            5,
            "f5_notice",
            "Recall Ondansetron OND-1188 (CDSCO alert)",
            {"batch_id": notable["ondan_recall"], "source": "regulator"},
            [("ondansetron", 1, {"batch_id": notable["ondan_recall"]})],
        )
        self.walk(a, None, None, None)  # waiting at Already dispensed to patients?

        b = self.case(
            5,
            "f5_notice",
            "Recall Pantoprazole PTZ-5502 (vendor notice)",
            {"batch_id": notable["panto_recall"], "source": "vendor"},
            [("pantoprazole", 1, {"batch_id": notable["panto_recall"]})],
        )
        self.walk(b, None)  # at Find affected batches; next step blocks and quarantines

        c = self.case(
            5,
            "f5_notice",
            "Recall Insulin INS-7781 (vendor notice)",
            {"batch_id": notable["insulin_80"], "source": "vendor"},
            [("insulin", 1, {"batch_id": notable["insulin_80"]})],
        )
        self.walk(c, None, None, None, "No", None)


def load(database_url: str, user_id: str, hospital_id: int) -> str:
    """Runs each stage in its own transaction and records it, so a dropped connection loses
    at most one stage and a rerun continues where it stopped."""
    # Keepalives: the load runs over the internet to the shared database.
    with psycopg.connect(
        database_url, keepalives=1, keepalives_idle=20, keepalives_interval=10
    ) as conn:
        seeder = Seeder(conn, user_id, hospital_id)
        row = seeder.cur.execute(
            "select value from public.hospital_settings where hospital_id = %s and key = %s",
            (hospital_id, PROGRESS_KEY),
        ).fetchone()
        done = list(row["value"]) if row else []
        if (
            not done
            and seeder.cur.execute(
                "select 1 from public.locations where hospital_id = %s limit 1", (hospital_id,)
            ).fetchone()
        ):
            return "this hospital already has locations that the demo loader did not create"
        conn.commit()
        for stage in STAGES:
            if stage in done:
                continue
            with conn.transaction():
                if stage == "masters":
                    seeder.masters()
                else:
                    seeder.reload_masters()
                    seeder.act_as_user()
                    if stage == "opening_stock":
                        seeder.opening_stock()
                    elif stage in ("expiry", "recall"):
                        getattr(seeder, stage)(seeder.notable())
                    else:
                        getattr(seeder, stage)()
                    seeder.cur.execute("reset role")
                done.append(stage)
                seeder.cur.execute(
                    "insert into public.hospital_settings (hospital_id, key, value)"
                    " values (%s, %s, %s::jsonb) on conflict (hospital_id, key)"
                    " do update set value = excluded.value",
                    (hospital_id, PROGRESS_KEY, json.dumps(done)),
                )
            print(f"stage done: {stage}", flush=True)
        counts = seeder.cur.execute(
            "select flow, count(*) as cases, count(distinct node) as nodes,"
            " count(*) filter (where closed) as closed from public.flow_cases"
            " where hospital_id = %s group by flow order by flow",
            (hospital_id,),
        ).fetchall()
    return "\n".join(
        f"flow {r['flow']}: {r['cases']} cases at {r['nodes']} steps, {r['closed']} closed"
        for r in counts
    )
