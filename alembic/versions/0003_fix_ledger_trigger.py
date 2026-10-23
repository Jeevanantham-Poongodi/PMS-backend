"""Fix the balance trigger for outgoing ledger lines.

0002 upserted with INSERT ... ON CONFLICT. PostgreSQL checks the proposed row against
`stock_balances_qty_check` before resolving the conflict, so every negative line failed even when
a balance row existed. Update first; insert only when there is no row yet. The check constraint
still refuses any balance that would go negative.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
create or replace function app.apply_ledger_line()
returns trigger
language plpgsql security definer
set search_path = public
as $$
begin
  update public.stock_balances set qty = qty + new.qty
  where hospital_id = new.hospital_id and batch_id = new.batch_id
    and location_id = new.location_id;
  if not found then
    insert into public.stock_balances (hospital_id, batch_id, location_id, item_id, qty)
    values (new.hospital_id, new.batch_id, new.location_id, new.item_id, new.qty);
  end if;
  return new;
end
$$;
"""

DOWNGRADE_SQL = """
create or replace function app.apply_ledger_line()
returns trigger
language plpgsql security definer
set search_path = public
as $$
begin
  insert into public.stock_balances (hospital_id, batch_id, location_id, item_id, qty)
  values (new.hospital_id, new.batch_id, new.location_id, new.item_id, new.qty)
  on conflict (hospital_id, batch_id, location_id)
  do update set qty = public.stock_balances.qty + excluded.qty;
  return new;
end
$$;
"""


def _raw(sql: str) -> None:
    op.get_bind().connection.cursor().execute(sql)


def upgrade() -> None:
    _raw(UPGRADE_SQL)


def downgrade() -> None:
    _raw(DOWNGRADE_SQL)
