-- Feature 002 Flow pages: locations, vendors, batches, settings, the stock ledger, flow cases and
-- the NDPS register (specs/002-flow-pages/data-model.md). Same rules as 0001: RLS on every
-- table, default grants revoked, only the privileges each table needs granted back.

-- ---------------------------------------------------------------------------
-- Masters
-- ---------------------------------------------------------------------------

create table public.locations (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  code         text not null,
  name         text not null,
  kind         text not null,
  allocatable  boolean not null default true,
  constraint locations_hospital_code_key unique (hospital_id, code),
  constraint locations_kind_check check (kind in ('central_store', 'pharmacy', 'ward', 'ot',
    'icu', 'quarantine_qc', 'quarantine_expiry', 'quarantine_recall')),
  -- Quarantine is a location that is never allocatable (constitution III).
  constraint locations_quarantine_check check (not (kind like 'quarantine_%' and allocatable))
);

create table public.vendors (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  name         text not null,
  active       boolean not null default true,
  constraint vendors_hospital_name_key unique (hospital_id, name)
);

create table public.batches (
  id                bigint generated always as identity primary key,
  hospital_id       bigint not null references public.hospitals (id),
  item_id           bigint not null references public.items (id),
  batch_no          text not null,
  expiry_date       date not null,
  vendor_id         bigint references public.vendors (id),
  blocked           boolean not null default false,
  first_receipt_at  timestamptz,
  constraint batches_item_batch_key unique (hospital_id, item_id, batch_no)
);

create table public.hospital_settings (
  hospital_id  bigint not null references public.hospitals (id),
  key          text not null,
  value        jsonb not null,
  primary key (hospital_id, key)
);

-- ---------------------------------------------------------------------------
-- Cases (the diagram's flows, research R1)
-- ---------------------------------------------------------------------------

create table public.flow_cases (
  id              bigint generated always as identity primary key,
  hospital_id     bigint not null references public.hospitals (id),
  flow            smallint not null,
  node            text not null,
  title           text not null,
  data            jsonb not null default '{}'::jsonb,
  parent_case_id  bigint references public.flow_cases (id),
  closed          boolean not null default false,
  raised_by       uuid not null default auth.uid(),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint flow_cases_flow_check check (flow >= 1 and flow <= 5)
);
create index flow_cases_hospital_flow_idx on public.flow_cases (hospital_id, flow, closed);

create table public.flow_case_lines (
  id             bigint generated always as identity primary key,
  hospital_id    bigint not null references public.hospitals (id),
  case_id        bigint not null references public.flow_cases (id),
  item_id        bigint not null references public.items (id),
  batch_id       bigint references public.batches (id),
  location_id    bigint references public.locations (id),
  qty_requested  numeric(12, 2) not null,
  qty_approved   numeric(12, 2)
);

create table public.flow_events (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  case_id      bigint not null references public.flow_cases (id),
  from_node    text,
  outcome      text,
  to_node      text not null,
  actor        uuid not null default auth.uid(),
  note         text,
  input        jsonb,
  created_at   timestamptz not null default now()
);

-- Two sign-offs; that they are different users is checked in domain code, relaxed in demo mode
-- (D-018).
create table public.ndps_register (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  case_id      bigint not null references public.flow_cases (id),
  item_id      bigint not null references public.items (id),
  batch_id     bigint references public.batches (id),
  qty          numeric(12, 2) not null,
  patient_ref  text not null,
  signoff_1    uuid not null,
  signoff_2    uuid not null,
  created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Stock: documents, append-only ledger, trigger-maintained balances (research R3)
-- ---------------------------------------------------------------------------

create table public.stock_documents (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  doc_type     text not null,
  case_id      bigint references public.flow_cases (id),
  patient_ref  text,
  prescriber   text,
  note         text,
  posted_by    uuid not null default auth.uid(),
  posted_at    timestamptz not null default now(),
  constraint stock_documents_type_check check (doc_type in ('opening', 'receipt', 'transfer',
    'issue', 'dispense', 'return', 'return_to_vendor', 'write_off'))
);

create table public.ledger_lines (
  id           bigint generated always as identity primary key,
  hospital_id  bigint not null references public.hospitals (id),
  document_id  bigint not null references public.stock_documents (id),
  item_id      bigint not null references public.items (id),
  batch_id     bigint not null references public.batches (id),
  location_id  bigint not null references public.locations (id),
  qty          numeric(12, 2) not null,
  posted_at    timestamptz not null default now(),
  constraint ledger_lines_qty_check check (qty <> 0)
);
create index ledger_lines_batch_idx on public.ledger_lines (hospital_id, batch_id);

create table public.stock_balances (
  hospital_id  bigint not null references public.hospitals (id),
  batch_id     bigint not null references public.batches (id),
  location_id  bigint not null references public.locations (id),
  item_id      bigint not null references public.items (id),
  qty          numeric(12, 2) not null,
  primary key (hospital_id, batch_id, location_id),
  -- A posting that would make any balance negative aborts its whole transaction (FR-012).
  constraint stock_balances_qty_check check (qty >= 0)
);

-- Keeps stock_balances equal to the sum of the ledger. SECURITY DEFINER: users cannot write
-- balances themselves.
create function app.apply_ledger_line()
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
revoke all on function app.apply_ledger_line() from public;

create trigger ledger_lines_apply
  after insert on public.ledger_lines
  for each row execute function app.apply_ledger_line();

create trigger stock_documents_append_only
  before update or delete on public.stock_documents
  for each row execute function app.forbid_update_delete();
create trigger ledger_lines_append_only
  before update or delete on public.ledger_lines
  for each row execute function app.forbid_update_delete();
create trigger flow_events_append_only
  before update or delete on public.flow_events
  for each row execute function app.forbid_update_delete();
create trigger ndps_register_append_only
  before update or delete on public.ndps_register
  for each row execute function app.forbid_update_delete();

-- ---------------------------------------------------------------------------
-- Row-level security and grants
-- ---------------------------------------------------------------------------

alter table public.locations         enable row level security;
alter table public.vendors           enable row level security;
alter table public.batches           enable row level security;
alter table public.hospital_settings enable row level security;
alter table public.flow_cases        enable row level security;
alter table public.flow_case_lines   enable row level security;
alter table public.flow_events       enable row level security;
alter table public.ndps_register     enable row level security;
alter table public.stock_documents   enable row level security;
alter table public.ledger_lines      enable row level security;
alter table public.stock_balances    enable row level security;

revoke all on public.locations, public.vendors, public.batches, public.hospital_settings,
  public.flow_cases, public.flow_case_lines, public.flow_events, public.ndps_register,
  public.stock_documents, public.ledger_lines, public.stock_balances from anon, authenticated;

grant select on public.locations, public.vendors, public.batches, public.hospital_settings,
  public.flow_cases, public.flow_case_lines, public.flow_events, public.ndps_register,
  public.stock_documents, public.ledger_lines, public.stock_balances to authenticated;
grant insert on public.locations, public.vendors, public.batches, public.hospital_settings,
  public.flow_cases, public.flow_case_lines, public.flow_events, public.ndps_register,
  public.stock_documents, public.ledger_lines to authenticated;
-- No UPDATE on the append-only tables, and no DELETE anywhere.
grant update on public.batches, public.hospital_settings, public.flow_cases,
  public.flow_case_lines to authenticated;

do $$
declare
  t text;
begin
  foreach t in array array['locations', 'vendors', 'batches', 'hospital_settings', 'flow_cases',
    'flow_case_lines', 'flow_events', 'ndps_register', 'stock_documents', 'ledger_lines',
    'stock_balances']
  loop
    execute format(
      'create policy %I on public.%I for select to authenticated'
      ' using (hospital_id in (select app.current_hospital_ids()))',
      t || '_member_read', t);
  end loop;

  foreach t in array array['locations', 'vendors', 'batches', 'hospital_settings', 'flow_cases',
    'flow_case_lines', 'flow_events', 'ndps_register', 'stock_documents', 'ledger_lines']
  loop
    execute format(
      'create policy %I on public.%I for insert to authenticated'
      ' with check (hospital_id in (select app.current_hospital_ids())'
      ' and app.has_permission(hospital_id, ''flows.write''))',
      t || '_insert', t);
  end loop;

  foreach t in array array['batches', 'hospital_settings', 'flow_cases', 'flow_case_lines']
  loop
    execute format(
      'create policy %I on public.%I for update to authenticated'
      ' using (hospital_id in (select app.current_hospital_ids())'
      ' and app.has_permission(hospital_id, ''flows.write''))'
      ' with check (hospital_id in (select app.current_hospital_ids())'
      ' and app.has_permission(hospital_id, ''flows.write''))',
      t || '_update', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------
-- Permissions and settings for every existing hospital
-- ---------------------------------------------------------------------------

insert into public.permissions (code, description) values
  ('flows.read',    'View flow cases'),
  ('flows.write',   'Raise and move flow cases, post stock'),
  ('flows.approve', 'Take approval outcomes in the flows'),
  ('stock.read',    'View stock balances'),
  ('masters.read',  'View locations, vendors and batches');

insert into public.role_permissions (hospital_id, role_code, permission_code)
select r.hospital_id, r.code, p.code
from public.roles r
cross join (values ('flows.read'), ('flows.write'), ('flows.approve'), ('stock.read'),
  ('masters.read')) as p (code)
where r.code = 'admin'
on conflict do nothing;

-- F4:"Within 90, 60 or 30 days?" windows (constitution VIII), and the D-012 temporary rule: a
-- batch is not allocatable on or after its expiry date (false would allow the date itself).
insert into public.hospital_settings (hospital_id, key, value)
select h.id, s.key, s.value
from public.hospitals h
cross join (values ('expiry.alert_windows_days', '[90, 60, 30]'::jsonb),
  ('allocation.block_on_expiry_date', 'true'::jsonb)) as s (key, value)
on conflict do nothing;
