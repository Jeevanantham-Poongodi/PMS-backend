from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
FormularyStatus = Literal["formulary", "non_formulary"]
ControlledClass = Literal["none", "narcotic", "schedule_x"]


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text
    base_unit: Text
    formulary_status: FormularyStatus
    controlled_class: ControlledClass


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Text | None = None
    base_unit: Text | None = None
    formulary_status: FormularyStatus | None = None
    controlled_class: ControlledClass | None = None
    active: bool | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class Item(BaseModel):
    id: int
    name: str
    base_unit: str
    formulary_status: FormularyStatus
    controlled_class: ControlledClass
    active: bool
    created_at: datetime
    updated_at: datetime


class Me(BaseModel):
    user_id: str
    hospital_id: int
    display_name: str
    roles: list[str]
    permissions: list[str]
    mfa_satisfied: bool


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable", "not_configured"]


# ---------------------------------------------------------------------------
# Feature 002 (specs/002-flow-pages/contracts/openapi.yaml)
# ---------------------------------------------------------------------------

Qty = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class NodeOut(BaseModel):
    key: str
    label: str
    kind: Literal["start", "task", "decision", "end"]


class EdgeOut(BaseModel):
    id: str
    source: str = Field(serialization_alias="from")
    target: str = Field(serialization_alias="to")
    outcome: str | None
    approval: bool
    reason: bool
    inputs: list[str]


class FlowDefinition(BaseModel):
    flow: int
    title: str
    starts: list[str]
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class LineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    qty: Qty
    batch_id: int | None = None
    location_id: int | None = None


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow: Annotated[int, Field(ge=1, le=5)]
    start: Text
    title: Text
    data: dict[str, Any] = {}
    lines: list[LineInput] = []


class CaseSummary(BaseModel):
    id: int
    flow: int
    node: str
    node_label: str
    title: str
    closed: bool
    parent_case_id: int | None
    created_at: datetime
    updated_at: datetime


class CaseLine(BaseModel):
    id: int
    item_id: int
    item_name: str
    batch_id: int | None
    batch_no: str | None
    expiry_date: date | None
    location_id: int | None
    location_name: str | None
    qty_requested: Decimal
    qty_approved: Decimal | None


class CaseEvent(BaseModel):
    id: int
    from_node: str | None
    outcome: str | None
    to_node: str
    actor_name: str
    note: str | None
    created_at: datetime


class CaseDetail(CaseSummary):
    data: dict[str, Any]
    lines: list[CaseLine]
    events: list[CaseEvent]
    actions: list[EdgeOut]
    child_case_ids: list[int]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge: Text
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    input: dict[str, Any] | None = None


class ScanResult(BaseModel):
    created: int
    case_ids: list[int]


class Location(BaseModel):
    id: int
    code: str
    name: str
    kind: str
    allocatable: bool


class Vendor(BaseModel):
    id: int
    name: str


class Batch(BaseModel):
    id: int
    item_id: int
    item_name: str
    batch_no: str
    expiry_date: date
    vendor_id: int | None
    blocked: bool


class Balance(BaseModel):
    location_id: int
    location_name: str
    location_kind: str
    item_id: int
    item_name: str
    batch_id: int
    batch_no: str
    expiry_date: date
    blocked: bool
    qty: Decimal


class Dispensed(BaseModel):
    patient_ref: str | None
    prescriber: str | None
    qty: Decimal
    posted_at: datetime


class BatchTrace(BaseModel):
    batch: Batch
    holdings: list[Balance]
    dispensed: list[Dispensed]


class NodeCount(BaseModel):
    flow: int
    node: str
    node_label: str
    count: int


class ExpiryBucket(BaseModel):
    window_days: int
    batches: int
    qty: Decimal


class QuarantineTotal(BaseModel):
    location_name: str
    qty: Decimal


class Dashboard(BaseModel):
    open_cases: list[NodeCount]
    near_expiry: list[ExpiryBucket]
    quarantine: list[QuarantineTotal]
