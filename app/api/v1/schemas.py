from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

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
