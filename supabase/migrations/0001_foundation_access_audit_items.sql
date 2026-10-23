-- Feature 001 Foundation, increment 1: tenancy, roles and permissions, audit trail, item master.
-- Every table has RLS. Supabase's default grants to anon/authenticated are revoked and only the
-- privileges each table needs are granted back (the anon key is public; see D-016).

create schema if not exists app;
grant usage on schema app to authenticated;

-- ---------------------------------------------------------------------------
-- Tenancy and access
-- ---------------------------------------------------------------------------

create table public.hospitals (
  id          bigint generated always as identity primary key,
  name        text not null check (length(btrim(name)) > 0),
  time_zone   text not null default 'Asia/Kolkata',
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

create table public.hospital_members (
  hospital_id   bigint not null references public.hospitals (id),
  user_id       uuid not null references auth.users (id),
  display_name  text not null check (length(btrim(display_name)) > 0),
  active        boolean not null default true,
  created_at    timestamptz not null default now(),
  primary key (hospital_id, user_id)
);

create table public.roles (
  hospital_id  bigint not null references public.hospitals (id),
  code         text not null,
  name         text not null,
  privileged   boolean not null default false,
  primary key (hospital_id, code)
);

create table public.permissions (
  code         text primary key,
  description  text not null
);

create table public.role_permissions (
  hospital_id      bigint not null,
  role_code        text not null,
  permission_code  text not null references public.permissions (code),
  primary key (hospital_id, role_code, permission_code),
  foreign key (hospital_id, role_code) references public.roles (hospital_id, code)
);

create table public.user_roles (
  hospital_id  bigint not null,
  user_id      uuid not null,
  role_code    text not null,
  primary key (hospital_id, user_id, role_code),
  foreign key (hospital_id, user_id) references public.hospital_members (hospital_id, user_id),
  foreign key (hospital_id, role_code) references public.roles (hospital_id, code)
);

-- Hospitals the signed-in user is an active member of. SECURITY DEFINER so policies on
-- hospital_members do not recurse.
create function app.current_hospital_ids()
returns setof bigint
language sql stable security definer
set search_path = public
as $$
  select hospital_id from public.hospital_members
  where user_id = auth.uid() and active
$$;

-- Whether the signed-in user holds a permission in a hospital through any of their roles.
create function app.has_permission(p_hospital_id bigint, p_permission text)
returns boolean
language sql stable security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.user_roles ur
    join public.hospital_members m
      on m.hospital_id = ur.hospital_id and m.user_id = ur.user_id and m.active
    join public.role_permissions rp
      on rp.hospital_id = ur.hospital_id and rp.role_code = ur.role_code
    where ur.hospital_id = p_hospital_id
      and ur.user_id = auth.uid()
      and rp.permission_code = p_permission
  )
$$;

revoke all on function app.current_hospital_ids() from public;
revoke all on function app.has_permission(bigint, text) from public;
grant execute on function app.current_hospital_ids() to authenticated;
grant execute on function app.has_permission(bigint, text) to authenticated;

alter table public.hospitals        enable row level security;
alter table public.hospital_members enable row level security;
alter table public.roles            enable row level security;
alter table public.permissions      enable row level security;
alter table public.role_permissions enable row level security;
alter table public.user_roles       enable row level security;

revoke all on public.hospitals, public.hospital_members, public.roles, public.permissions,
  public.role_permissions, public.user_roles from anon, authenticated;
grant select on public.hospitals, public.hospital_members, public.roles, public.permissions,
  public.role_permissions, public.user_roles to authenticated;

create policy hospitals_member_read on public.hospitals for select to authenticated
  using (id in (select app.current_hospital_ids()));
create policy members_member_read on public.hospital_members for select to authenticated
  using (hospital_id in (select app.current_hospital_ids()));
create policy roles_member_read on public.roles for select to authenticated
  using (hospital_id in (select app.current_hospital_ids()));
create policy permissions_read on public.permissions for select to authenticated
  using (true);
create policy role_permissions_member_read on public.role_permissions for select to authenticated
  using (hospital_id in (select app.current_hospital_ids()));
create policy user_roles_member_read on public.user_roles for select to authenticated
  using (hospital_id in (select app.current_hospital_ids()));

insert into public.permissions (code, description) values
  ('items.read',  'View the item master'),
  ('items.write', 'Create and change items'),
  ('audit.read',  'Read and export the audit trail');

-- ---------------------------------------------------------------------------
-- Append-only, hash-chained audit trail (constitution VII)
-- ---------------------------------------------------------------------------

create table public.audit_entries (
  id             bigint generated always as identity primary key,
  hospital_id    bigint not null references public.hospitals (id),
  actor_user_id  uuid,
  system_job     text,
  action         text not null,
  entity_type    text not null,
  entity_id      text not null,
  before         jsonb,
  after          jsonb,
  reason         text,
  request_id     text,
  created_at     timestamptz not null default now(),
  prev_hash      text,
  entry_hash     text not null,
  check (actor_user_id is not null or system_job is not null)
);
create index audit_entries_hospital_id_idx on public.audit_entries (hospital_id, id);

create function app.forbid_update_delete()
returns trigger
language plpgsql
as $$
begin
  raise exception '% is append-only', tg_table_name using errcode = 'insufficient_privilege';
end
$$;

create trigger audit_entries_append_only
  before update or delete on public.audit_entries
  for each row execute function app.forbid_update_delete();

-- The only way to write an audit entry: checks membership, chains the hash under a per-hospital
-- lock, and records the signed-in user as the actor.
create function app.audit_append(
  p_hospital_id bigint, p_action text, p_entity_type text, p_entity_id text,
  p_before jsonb, p_after jsonb, p_reason text, p_request_id text
)
returns bigint
language plpgsql security definer
set search_path = public
as $$
declare
  v_prev   text;
  v_hash   text;
  v_now    timestamptz := now();
  v_id     bigint;
begin
  if auth.uid() is null or p_hospital_id not in (select app.current_hospital_ids()) then
    raise exception 'not a member of hospital %', p_hospital_id using errcode = 'insufficient_privilege';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('audit_entries', p_hospital_id));
  select entry_hash into v_prev from public.audit_entries
    where hospital_id = p_hospital_id order by id desc limit 1;
  v_hash := encode(sha256(convert_to(concat_ws('|',
    coalesce(v_prev, ''), p_hospital_id::text, auth.uid()::text, p_action, p_entity_type,
    p_entity_id, coalesce(p_before::text, ''), coalesce(p_after::text, ''),
    coalesce(p_reason, ''), v_now::text), 'UTF8')), 'hex');
  insert into public.audit_entries (hospital_id, actor_user_id, action, entity_type, entity_id,
    before, after, reason, request_id, created_at, prev_hash, entry_hash)
  values (p_hospital_id, auth.uid(), p_action, p_entity_type, p_entity_id, p_before, p_after,
    p_reason, p_request_id, v_now, v_prev, v_hash)
  returning id into v_id;
  return v_id;
end
$$;

revoke all on function app.audit_append(bigint, text, text, text, jsonb, jsonb, text, text) from public;
grant execute on function app.audit_append(bigint, text, text, text, jsonb, jsonb, text, text)
  to authenticated;

alter table public.audit_entries enable row level security;
revoke all on public.audit_entries from anon, authenticated;
grant select on public.audit_entries to authenticated;
create policy audit_entries_auditor_read on public.audit_entries for select to authenticated
  using (app.has_permission(hospital_id, 'audit.read'));

-- ---------------------------------------------------------------------------
-- Item master (F1:"Formulary item?", F3:"Narcotic or Schedule X?")
-- ---------------------------------------------------------------------------

create table public.items (
  id                bigint generated always as identity primary key,
  hospital_id       bigint not null references public.hospitals (id),
  name              text not null check (length(btrim(name)) > 0),
  base_unit         text not null check (length(btrim(base_unit)) > 0),
  formulary_status  text not null check (formulary_status in ('formulary', 'non_formulary')),
  controlled_class  text not null check (controlled_class in ('none', 'narcotic', 'schedule_x')),
  active            boolean not null default true,
  created_by        uuid not null default auth.uid(),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create unique index items_hospital_name_key on public.items (hospital_id, lower(name));

alter table public.items enable row level security;
revoke all on public.items from anon, authenticated;
-- No DELETE grant: items are deactivated, never deleted (FR-015).
grant select, insert, update on public.items to authenticated;

create policy items_read on public.items for select to authenticated
  using (hospital_id in (select app.current_hospital_ids())
         and app.has_permission(hospital_id, 'items.read'));
create policy items_insert on public.items for insert to authenticated
  with check (hospital_id in (select app.current_hospital_ids())
              and app.has_permission(hospital_id, 'items.write')
              and created_by = auth.uid());
create policy items_update on public.items for update to authenticated
  using (hospital_id in (select app.current_hospital_ids())
         and app.has_permission(hospital_id, 'items.write'))
  with check (hospital_id in (select app.current_hospital_ids())
              and app.has_permission(hospital_id, 'items.write'));
