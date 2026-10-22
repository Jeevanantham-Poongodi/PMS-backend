"""SQLAlchemy models: the table definitions Alembic compares against to generate migrations.

The API still queries with psycopg so RLS applies per user (see app.core.db); these models
describe the schema only. Things a model cannot express (RLS policies, grants, functions,
triggers) go into the migration by hand with op.execute().
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Table,
    Text,
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