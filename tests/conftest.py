"""Shared harness for feature 002 database tests.

`flow_env` builds a throwaway hospital (locations, vendor, items, a user with every flow
permission) inside one transaction that is rolled back, and acts as that user so RLS applies.
Tests drive cases through the real domain services. Needs DATABASE_URL with migrations applied.
"""

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.auth import CurrentUser
from app.core.config import Settings, get_settings
from app.domain import flows, stock
from app.flows.definitions import FLOWS
from app.repositories import stock as stock_repo

LOCATIONS = [
    ("CS", "Central Store", "central_store", True),
    ("PH", "Main Pharmacy", "pharmacy", True),
    ("WA", "Ward A", "ward", True),
    ("ICU", "ICU", "icu", True),
    ("QQC", "QC Quarantine", "quarantine_qc", False),
    ("QEX", "Expiry Quarantine", "quarantine_expiry", False),
    ("QRC", "Recall Quarantine", "quarantine_recall", False),
]
ITEMS = {
    "para": ("Paracetamol 500 mg", "formulary", "none"),
    "amox": ("Amoxicillin 250 mg", "formulary", "none"),
    "morph": ("Morphine 10 mg inj", "formulary", "narcotic"),
    "nonf": ("Brand X Syrup", "non_formulary", "none"),
}
PERMISSIONS = ["flows.read", "flows.write", "flows.approve", "stock.read", "masters.read"]


@dataclass
class FlowEnv:
    conn: psycopg.Connection
    cur: psycopg.Cursor
    user: CurrentUser
    hospital_id: int
    loc: dict[str, int]
    items: dict[str, int]
    vendor_id: int

    def today(self) -> date:
        return self.cur.execute("select (now() at time zone 'Asia/Kolkata')::date as d").fetchone()[
            "d"
        ]

    def stock_in(self, kind: str, item: str, batch_no: str, days: int, qty) -> int:
        """Opening stock (D-017) of a new batch expiring in `days` days."""
        batch_id = self.cur.execute(
            "insert into public.batches (hospital_id, item_id, batch_no, expiry_date, vendor_id)"
            " values (%s, %s, %s, %s, %s) returning id",
            (
                self.hospital_id,
                self.items[item],
                batch_no,
                self.today() + timedelta(days=days),
                self.vendor_id,
            ),
        ).fetchone()["id"]
        stock.post_document(
            self.cur,
            self.user,
            "opening",
            [
                {
                    "item_id": self.items[item],
                    "batch_id": batch_id,
                    "location_id": self.loc[kind],
                    "qty": Decimal(qty),
                }
            ],
            request_id=None,
        )
        return batch_id

    def balance(self, batch_id: int, kind: str) -> Decimal:
        return stock_repo.balance(self.cur, self.hospital_id, batch_id, self.loc[kind])

    def create(self, flow: int, start: str, data: dict | None = None, lines=()) -> int:
        """lines: (item key, qty) or (item key, qty, {batch_id/location_id})."""
        rows = []
        for item, qty, *extra in lines:
            rows.append({"item_id": self.items[item], "qty": qty, **(extra[0] if extra else {})})
        case = flows.open_case(
            self.cur, self.user, flow, start, f"Test case flow {flow}", data or {}, rows, None
        )
        return case["id"]

    def case(self, case_id: int) -> dict:
        return flows._detail(
            self.cur, self.user, flows.repo.get_case(self.cur, self.hospital_id, case_id)
        )

    def node(self, case_id: int) -> str:
        return self.case(case_id)["node"]

    def outcomes(self, case_id: int) -> list[str | None]:
        return [e.outcome for e in self.case(case_id)["actions"]]

    def act(self, case_id: int, outcome: str | None = None, note=None, **input_) -> dict:
        case = flows.repo.get_case(self.cur, self.hospital_id, case_id)
        flow = FLOWS[case["flow"]]
        edge = next((e for e in flow.outgoing(case["node"]) if e.outcome == outcome), None)
        assert edge is not None, f"no arrow {outcome!r} from {case['node']}"
        with self.conn.transaction():
            return flows.move(self.cur, self.user, case_id, edge.id, note, input_ or None, None)

    def line_ids(self, case_id: int) -> list[int]:
        return [line["id"] for line in self.case(case_id)["lines"]]

    def lines(self, case_id: int) -> list[dict]:
        return self.case(case_id)["lines"]


@pytest.fixture(autouse=True)
def fresh_user_cache():
    """Tests reuse fake tokens with different users; never serve a cached one."""
    from app.core.auth import clear_user_cache

    clear_user_cache()
    yield
    clear_user_cache()


@pytest.fixture
def flow_env():
    url = get_settings().database_url
    if not url:
        pytest.skip("DATABASE_URL not set")
    with psycopg.connect(url) as conn:
        if conn.execute("select to_regclass('public.flow_cases')").fetchone()[0] is None:
            pytest.skip("migration 0002 not applied")
        conn.execute("begin")
        try:
            yield _build(conn)
        finally:
            conn.rollback()


def _build(conn: psycopg.Connection) -> FlowEnv:
    user_id = uuid.uuid4()
    conn.execute(
        "insert into auth.users (id, aud, role, email) values (%s, 'authenticated',"
        " 'authenticated', %s)",
        (user_id, f"flow-{user_id}@test.invalid"),
    )
    hospital_id = conn.execute(
        "insert into public.hospitals (name) values ('Flow Test Hospital') returning id"
    ).fetchone()[0]
    conn.execute(
        "insert into public.roles (hospital_id, code, name) values (%s, 'store', 'Store')",
        (hospital_id,),
    )
    for permission in PERMISSIONS + ["items.read", "items.write"]:
        conn.execute(
            "insert into public.role_permissions values (%s, 'store', %s)",
            (hospital_id, permission),
        )
    conn.execute(
        "insert into public.hospital_members (hospital_id, user_id, display_name)"
        " values (%s, %s, 'Tester')",
        (hospital_id, user_id),
    )
    conn.execute("insert into public.user_roles values (%s, %s, 'store')", (hospital_id, user_id))
    loc = {}
    for code, name, kind, allocatable in LOCATIONS:
        loc[kind] = conn.execute(
            "insert into public.locations (hospital_id, code, name, kind, allocatable)"
            " values (%s, %s, %s, %s, %s) returning id",
            (hospital_id, code, name, kind, allocatable),
        ).fetchone()[0]
    vendor_id = conn.execute(
        "insert into public.vendors (hospital_id, name) values (%s, 'Test Pharma') returning id",
        (hospital_id,),
    ).fetchone()[0]
    items = {}
    for key, (name, formulary, controlled) in ITEMS.items():
        items[key] = conn.execute(
            "insert into public.items (hospital_id, name, base_unit, formulary_status,"
            " controlled_class, created_by) values (%s, %s, 'unit', %s, %s, %s) returning id",
            (hospital_id, name, formulary, controlled, user_id),
        ).fetchone()[0]

    claims = {"sub": str(user_id), "role": "authenticated"}
    conn.execute("set local role authenticated")
    conn.execute(
        "select set_config('request.jwt.claims', %s, true),"
        " set_config('request.jwt.claim.sub', %s, true)",
        (json.dumps(claims), str(user_id)),
    )
    user = CurrentUser(
        user_id=str(user_id),
        hospital_id=hospital_id,
        display_name="Tester",
        roles=frozenset({"store"}),
        permissions=frozenset(PERMISSIONS),
        privileged=False,
        mfa_satisfied=True,
        claims=claims,
    )
    return FlowEnv(
        conn, conn.cursor(row_factory=dict_row), user, hospital_id, loc, items, vendor_id
    )


@pytest.fixture
def strict_mode(monkeypatch):
    """Demo mode off: raiser ≠ approver and two NDPS signatories apply (D-018)."""
    strict = Settings(database_url=None, demo_mode=False)
    monkeypatch.setattr(flows, "get_settings", lambda: strict)
    monkeypatch.setattr("app.domain.effects.get_settings", lambda: strict)


@pytest.fixture
def tx_factory(flow_env):
    @contextmanager
    def tx():
        with flow_env.conn.transaction():
            yield flow_env.cur

    return tx
