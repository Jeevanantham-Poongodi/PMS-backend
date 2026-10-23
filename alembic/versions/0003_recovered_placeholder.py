"""Placeholder for revision 0003, whose file was lost.

The shared database records revision 0003 as applied, but its migration file was not recovered
with the rest of feature 002 (2026-10-23). Every schema object in that database is accounted for
by 0001 and 0002, so this revision does nothing; it keeps the revision chain intact so
`alembic upgrade head` and `alembic check` work. Replace it if the original file turns up.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from collections.abc import Sequence

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
