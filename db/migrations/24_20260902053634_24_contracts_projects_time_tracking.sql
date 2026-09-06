-- Supabase migration 20260902053634  24_contracts_projects_time_tracking
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- What happens AFTER a deal is won. Migration 20 added clients and deliverables; this
-- adds the three things that were still missing: the signed agreement, the project the
-- work is organised into, and the hours actually spent against it.

-- ---------------------------------------------------------------- contracts
create table if not exists public.contracts (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid references public.clients(id) on delete cascade,
  lead_id        uuid references public.leads(id) on delete set null,
  offer_id       uuid references public.offers(id) on delete set null,
  title          text not null,
  reference      text unique,
  value          numeric(12,2) not null check (value >= 0),
  currency       text not null default 'USD',
  billing_type   text not null default 'fixed'
                 check (billing_type in ('fixed','hourly','retainer','milestone')),
  hourly_rate    numeric(10,2),
  starts_on      date,
  ends_on        date,
  -- draft, out for signature, live, finished, or ended early. A contract without a
  -- state is a document; with one it is something the system can act on.
  status         text not null default 'draft'
                 check (status in ('draft','sent','signed','active','completed','terminated')),
  signed_at      timestamptz,
  signed_by      text,
  document_url   text,
  auto_renew     boolean not null default false,
  notes          text,
  created_at     timestamptz not null default now(),
  constraint contract_dates check (ends_on is null or starts_on is null or ends_on >= starts_on),
  -- An hourly contract without a rate cannot be invoiced, so the database refuses it
  -- rather than letting it surface as a zero on a report months later.
  constraint hourly_needs_rate check (billing_type <> 'hourly' or hourly_rate is not null)
);

-- ---------------------------------------------------------------- projects
create table if not exists public.projects (
  id           uuid primary key default gen_random_uuid(),
  client_id    uuid not null references public.clients(id) on delete cascade,
  contract_id  uuid references public.contracts(id) on delete set null,
  name         text not null,
  description  text,
  owner_id     uuid references public.representatives(id) on delete set null,
  status       text not null default 'planning'
               check (status in ('planning','active','on_hold','review','completed','cancelled')),
  health       text not null default 'on_track'
               check (health in ('on_track','at_risk','off_track')),
  starts_on    date,
  due_on       date,
  completed_at timestamptz,
  budget_hours numeric(8,2),
  created_at   timestamptz not null default now()
);

create table if not exists public.project_tasks (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references public.projects(id) on delete cascade,
  title        text not null,
  description  text,
  assignee_id  uuid references public.representatives(id) on delete set null,
  status       text not null default 'todo'
               check (status in ('todo','in_progress','blocked','review','done')),
  priority     text not null default 'medium' check (priority in ('low','medium','high')),
  estimate_hours numeric(6,2),
  due_on       date,
  completed_at timestamptz,
  position     integer not null default 0,
  created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------- time tracking
create table if not exists public.time_entries (
  id           uuid primary key default gen_random_uuid(),
  rep_id       uuid not null references public.representatives(id) on delete cascade,
  project_id   uuid references public.projects(id) on delete cascade,
  task_id      uuid references public.project_tasks(id) on delete set null,
  client_id    uuid references public.clients(id) on delete set null,
  entry_date   date not null default current_date,
  minutes      integer not null check (minutes > 0 and minutes <= 1440),
  billable     boolean not null default true,
  description  text,
  -- Once invoiced an entry is frozen, otherwise edits silently change an invoice that
  -- has already gone out.
  locked       boolean not null default false,
  created_at   timestamptz not null default now()
);

create or replace function public.freeze_locked_time_entry()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if old.locked and (new.minutes is distinct from old.minutes
                     or new.entry_date is distinct from old.entry_date
                     or new.billable is distinct from old.billable) then
    raise exception 'This time entry is locked because it has been invoiced.';
  end if;
  return new;
end;
$$;

revoke all on function public.freeze_locked_time_entry() from public, anon, authenticated;

drop trigger if exists trg_freeze_time_entry on public.time_entries;
create trigger trg_freeze_time_entry
  before update on public.time_entries
  for each row execute function public.freeze_locked_time_entry();

-- ---------------------------------------------------------------- policies
do $$
declare t text;
begin
  foreach t in array array['contracts','projects','project_tasks','time_entries'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from anon', t);
  end loop;
end $$;

-- Contracts and projects follow the client's account owner.
drop policy if exists contracts_access on public.contracts;
create policy contracts_access on public.contracts for all to authenticated
  using (
    public.current_role_name() in ('admin','manager')
    or exists (select 1 from public.clients c
               where c.id = contracts.client_id and c.account_owner = public.my_rep_id())
  )
  with check (
    public.current_role_name() in ('admin','manager')
    or exists (select 1 from public.clients c
               where c.id = contracts.client_id and c.account_owner = public.my_rep_id())
  );

drop policy if exists projects_access on public.projects;
create policy projects_access on public.projects for all to authenticated
  using (
    public.current_role_name() in ('admin','manager')
    or owner_id = public.my_rep_id()
    or exists (select 1 from public.clients c
               where c.id = projects.client_id and c.account_owner = public.my_rep_id())
  )
  with check (
    public.current_role_name() in ('admin','manager')
    or owner_id = public.my_rep_id()
    or exists (select 1 from public.clients c
               where c.id = projects.client_id and c.account_owner = public.my_rep_id())
  );

-- A task is visible to whoever can see its project, plus its own assignee.
drop policy if exists tasks_access on public.project_tasks;
create policy tasks_access on public.project_tasks for all to authenticated
  using (
    public.current_role_name() in ('admin','manager')
    or assignee_id = public.my_rep_id()
    or exists (select 1 from public.projects p
               where p.id = project_tasks.project_id and p.owner_id = public.my_rep_id())
  )
  with check (
    public.current_role_name() in ('admin','manager')
    or assignee_id = public.my_rep_id()
    or exists (select 1 from public.projects p
               where p.id = project_tasks.project_id and p.owner_id = public.my_rep_id())
  );

-- Time is personal. A representative sees and logs only their own; managers see all.
drop policy if exists time_access on public.time_entries;
create policy time_access on public.time_entries for all to authenticated
  using (public.current_role_name() in ('admin','manager') or rep_id = public.my_rep_id())
  with check (public.current_role_name() in ('admin','manager') or rep_id = public.my_rep_id());

create index if not exists idx_contracts_client on public.contracts (client_id, status);
create index if not exists idx_projects_client  on public.projects (client_id, status);
create index if not exists idx_tasks_project    on public.project_tasks (project_id, status, position);
create index if not exists idx_time_rep_date    on public.time_entries (rep_id, entry_date desc);
create index if not exists idx_time_project     on public.time_entries (project_id, entry_date desc);

-- ---------------------------------------------------------------- rollup
-- Hours logged, hours budgeted and how much of the contract is consumed, per project,
-- computed in the database so every screen and report agrees on the number.
create or replace view public.v_project_health
with (security_invoker = true) as
select
  p.id, p.name, p.status, p.health, p.due_on, p.budget_hours,
  c.name  as client_name,
  ct.title as contract_title,
  ct.value as contract_value,
  ct.currency,
  ct.billing_type,
  r.name  as owner_name,
  coalesce(round(sum(te.minutes) / 60.0, 2), 0)                          as hours_logged,
  coalesce(round(sum(te.minutes) filter (where te.billable) / 60.0, 2), 0) as billable_hours,
  case when p.budget_hours > 0
       then round((coalesce(sum(te.minutes), 0) / 60.0) / p.budget_hours * 100, 0)
       else null end                                                      as budget_used_pct,
  count(distinct t.id)                                                    as task_count,
  count(distinct t.id) filter (where t.status = 'done')                   as tasks_done
from public.projects p
left join public.clients c        on c.id = p.client_id
left join public.contracts ct     on ct.id = p.contract_id
left join public.representatives r on r.id = p.owner_id
left join public.time_entries te  on te.project_id = p.id
left join public.project_tasks t  on t.project_id = p.id
group by p.id, p.name, p.status, p.health, p.due_on, p.budget_hours,
         c.name, ct.title, ct.value, ct.currency, ct.billing_type, r.name;

revoke all on public.v_project_health from anon;
grant select on public.v_project_health to authenticated;
