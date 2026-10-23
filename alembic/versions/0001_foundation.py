"""Foundation: tenancy, roles and permissions, audit trail, item master.

Baseline revision. It runs the original SQL file unchanged because most of it (RLS policies,
grants, security-definer functions, the append-only trigger) cannot be expressed as models.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = (
    Path(__file__).resolve().parents[2]
    / "supabase"
    / "migrations"
    / "0001_foundation_access_audit_items.sql"
)


def upgrade() -> None:
    # Straight to the driver: the SQL has %, :: casts and $$ bodies that bind-parameter parsing
    # would misread.
    op.get_bind().connection.cursor().execute(SQL_FILE.read_text())


def downgrade() -> None:
    op.execute(
        "drop table if exists public.items, public.audit_entries, public.user_roles,"
        " public.role_permissions, public.permissions, public.roles, public.hospital_members,"
        " public.hospitals cascade"
    )
    op.execute(
        "drop function if exists app.audit_append, app.forbid_update_delete,"
        " app.has_permission, app.current_hospital_ids"
    )
