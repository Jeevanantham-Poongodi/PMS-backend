"""Flow pages: locations, vendors, batches, settings, stock ledger, flow cases, NDPS register.

Runs supabase/migrations/0002_flow_pages.sql, like 0001, because RLS policies, grants, the
ledger trigger and the seeded permissions cannot be expressed as models.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = (
    Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0002_flow_pages.sql"
)


def upgrade() -> None:
    # Straight to the driver: the SQL has %, :: casts and $$ bodies that bind-parameter parsing
    # would misread.
    op.get_bind().connection.cursor().execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "drop table if exists public.ledger_lines, public.stock_balances, public.stock_documents,"
        " public.ndps_register, public.flow_events, public.flow_case_lines, public.flow_cases,"
        " public.hospital_settings, public.batches, public.vendors, public.locations cascade"
    )
    op.execute("drop function if exists app.apply_ledger_line")
    op.execute(
        "delete from public.role_permissions where permission_code in"
        " ('flows.read', 'flows.write', 'flows.approve', 'stock.read', 'masters.read')"
    )
    op.execute(
        "delete from public.permissions where code in"
        " ('flows.read', 'flows.write', 'flows.approve', 'stock.read', 'masters.read')"
    )
