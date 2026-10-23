"""SQLAlchemy models: the table definitions Alembic compares against to generate migrations.

The API still queries with psycopg so RLS applies per user (see app.core.db); these models
describe the schema only. Things a model cannot express (RLS policies, grants, functions,
triggers) go into the migration by hand with op.execute().
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Supabase's sign-in accounts. Declared only so foreign keys resolve; Alembic never manages it.
auth_users = Table(
    "users", Base.metadata, Column("id", UUID(as_uuid=True), primary_key=True), schema="auth"
)


class Hospital(Base):
    __tablename__ = "hospitals"
    __table_args__ = (CheckConstraint("length(btrim(name)) > 0", name="hospitals_name_check"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    time_zone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Kolkata'"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HospitalMember(Base):
    __tablename__ = "hospital_members"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(display_name)) > 0", name="hospital_members_display_name_check"
        ),
    )

    hospital_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hospitals.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "roles"

    hospital_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hospitals.id"), primary_key=True
    )
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    privileged: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Permission(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    description: Mapped[str] = mapped_column(Text)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(["hospital_id", "role_code"], ["roles.hospital_id", "roles.code"]),
    )

    hospital_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_code: Mapped[str] = mapped_column(Text, primary_key=True)
    permission_code: Mapped[str] = mapped_column(
        Text, ForeignKey("permissions.code"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["hospital_id", "user_id"],
            ["hospital_members.hospital_id", "hospital_members.user_id"],
        ),
        ForeignKeyConstraint(["hospital_id", "role_code"], ["roles.hospital_id", "roles.code"]),
    )

    hospital_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role_code: Mapped[str] = mapped_column(Text, primary_key=True)


class AuditEntry(Base):
    """Append-only (trigger) and written only through app.audit_append()."""

    __tablename__ = "audit_entries"
    __table_args__ = (
        CheckConstraint(
            "actor_user_id is not null or system_job is not null", name="audit_entries_check"
        ),
        Index("audit_entries_hospital_id_idx", "hospital_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    system_job: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    prev_hash: Mapped[str | None] = mapped_column(Text)
    entry_hash: Mapped[str] = mapped_column(Text)


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="items_name_check"),
        CheckConstraint("length(btrim(base_unit)) > 0", name="items_base_unit_check"),
        CheckConstraint(
            "formulary_status in ('formulary', 'non_formulary')",
            name="items_formulary_status_check",
        ),
        CheckConstraint(
            "controlled_class in ('none', 'narcotic', 'schedule_x')",
            name="items_controlled_class_check",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    name: Mapped[str] = mapped_column(Text)
    base_unit: Mapped[str] = mapped_column(Text)
    formulary_status: Mapped[str] = mapped_column(Text)
    controlled_class: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("auth.uid()")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("items_hospital_name_key", Item.hospital_id, func.lower(Item.name), unique=True)


# ---------------------------------------------------------------------------
# Feature 002: masters, stock ledger and flow cases (specs/002-flow-pages/data-model.md)
# ---------------------------------------------------------------------------

LOCATION_KINDS = (
    "central_store",
    "pharmacy",
    "ward",
    "ot",
    "icu",
    "quarantine_qc",
    "quarantine_expiry",
    "quarantine_recall",
)
DOC_TYPES = (
    "opening",
    "receipt",
    "transfer",
    "issue",
    "dispense",
    "return",
    "return_to_vendor",
    "write_off",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in ({', '.join(repr(v) for v in values)})"


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("hospital_id", "code", name="locations_hospital_code_key"),
        CheckConstraint(_in("kind", LOCATION_KINDS), name="locations_kind_check"),
        # Quarantine is a location that is never allocatable (constitution III).
        CheckConstraint(
            "not (kind like 'quarantine_%' and allocatable)", name="locations_quarantine_check"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    allocatable: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("hospital_id", "name", name="vendors_hospital_name_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        UniqueConstraint("hospital_id", "item_id", "batch_no", name="batches_item_batch_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id"))
    batch_no: Mapped[str] = mapped_column(Text)
    expiry_date: Mapped[date] = mapped_column(Date)
    vendor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("vendors.id"))
    blocked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # FEFO tie-break, D-005 step 2: set at the batch's first receipt.
    first_receipt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HospitalSetting(Base):
    __tablename__ = "hospital_settings"

    hospital_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hospitals.id"), primary_key=True
    )
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class FlowCase(Base):
    __tablename__ = "flow_cases"
    __table_args__ = (CheckConstraint("flow between 1 and 5", name="flow_cases_flow_check"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    flow: Mapped[int] = mapped_column(SmallInteger)
    node: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    parent_case_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("flow_cases.id"))
    closed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    raised_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("auth.uid()")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("flow_cases_hospital_flow_idx", FlowCase.hospital_id, FlowCase.flow, FlowCase.closed)


class FlowCaseLine(Base):
    __tablename__ = "flow_case_lines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flow_cases.id"))
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("batches.id"))
    location_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("locations.id"))
    qty_requested: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    qty_approved: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class FlowEvent(Base):
    """Append-only history of a case's moves through the diagram."""

    __tablename__ = "flow_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flow_cases.id"))
    from_node: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    to_node: Mapped[str] = mapped_column(Text)
    actor: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), server_default=text("auth.uid()"))
    note: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StockDocument(Base):
    """A posted stock movement (constitution II). Append-only."""

    __tablename__ = "stock_documents"
    __table_args__ = (
        CheckConstraint(_in("doc_type", DOC_TYPES), name="stock_documents_type_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    doc_type: Mapped[str] = mapped_column(Text)
    case_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("flow_cases.id"))
    patient_ref: Mapped[str | None] = mapped_column(Text)
    prescriber: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    posted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), server_default=text("auth.uid()")
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerLine(Base):
    """Signed quantity per item, batch and location. Append-only; balances follow by trigger."""

    __tablename__ = "ledger_lines"
    __table_args__ = (CheckConstraint("qty <> 0", name="ledger_lines_qty_check"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stock_documents.id"))
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id"))
    batch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("batches.id"))
    location_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("locations.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


Index("ledger_lines_batch_idx", LedgerLine.hospital_id, LedgerLine.batch_id)


class StockBalance(Base):
    """Maintained only by the ledger trigger. The check makes any negative posting fail."""

    __tablename__ = "stock_balances"
    __table_args__ = (CheckConstraint("qty >= 0", name="stock_balances_qty_check"),)

    hospital_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("hospitals.id"), primary_key=True
    )
    batch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("batches.id"), primary_key=True)
    location_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("locations.id"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class NdpsRegister(Base):
    """NDPS / Schedule X register (constitution VI). Append-only."""

    __tablename__ = "ndps_register"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    hospital_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("hospitals.id"))
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("flow_cases.id"))
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("batches.id"))
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    patient_ref: Mapped[str] = mapped_column(Text)
    signoff_1: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    signoff_2: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
