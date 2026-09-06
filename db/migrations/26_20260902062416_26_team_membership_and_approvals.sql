-- Supabase migration 20260902062416  26_team_membership_and_approvals
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Two things a business owner needs that the schema could not express.
--
-- 1. WHO IS ON WHAT. A representative had leads and time entries, but nothing said which
--    projects they belong to, so "attach this person to that project" had nowhere to be
--    recorded.
-- 2. WHAT NEEDS A DECISION. Unassigned leads were counted on the dashboard and then had
--    nowhere to go. A queue with no verbs is a number, not a workflow.

-- ---------------------------------------------------------------- project membership
create table if not exists public.project_members (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references public.projects(id) on delete cascade,
  rep_id      uuid not null references public.representatives(id) on delete cascade,
  role        text not null default 'contributor'
              check (role in ('lead','contributor','reviewer','observer')),
  allocation_pct integer not null default 100 check (allocation_pct between 1 and 100),
  added_by    uuid references auth.users(id) on delete set null,
  added_at    timestamptz not null default now(),
  unique (project_id, rep_id)
);

alter table public.project_members enable row level security;
revoke all on table public.project_members from anon;

drop policy if exists project_members_read on public.project_members;
create policy project_members_read on public.project_members
  for select to authenticated using (true);

drop policy if exists project_members_write on public.project_members;
create policy project_members_write on public.project_members
  for all to authenticated
  using (public.current_role_name() in ('admin','manager'))
  with check (public.current_role_name() in ('admin','manager'));

create index if not exists idx_project_members_rep on public.project_members (rep_id);

-- ---------------------------------------------------------------- approvals
-- An unassigned lead is a decision waiting to happen. This records the decision, who
-- made it, and why, so "why is this lead sitting here" always has an answer.
create table if not exists public.approvals (
  id           uuid primary key default gen_random_uuid(),
  lead_id      uuid not null references public.leads(id) on delete cascade,
  kind         text not null default 'assignment'
               check (kind in ('assignment','discount','escalation','refund')),
  -- pending is waiting on anyone; escalated is waiting specifically on an administrator.
  status       text not null default 'pending'
               check (status in ('pending','escalated','approved','rejected','on_hold')),
  requested_by uuid references auth.users(id) on delete set null,
  decided_by   uuid references auth.users(id) on delete set null,
  decided_at   timestamptz,
  reason       text,
  note         text,
  created_at   timestamptz not null default now(),
  unique (lead_id, kind)
);

alter table public.approvals enable row level security;
revoke all on table public.approvals from anon;

drop policy if exists approvals_read on public.approvals;
create policy approvals_read on public.approvals
  for select to authenticated
  using (
    public.current_role_name() in ('admin','manager')
    or exists (select 1 from public.leads l
               where l.id = approvals.lead_id and l.assigned_rep_id = public.my_rep_id())
  );

-- Anyone signed in may RAISE one. Only a manager or an administrator may decide it,
-- which is the whole point of an approval.
drop policy if exists approvals_raise on public.approvals;
create policy approvals_raise on public.approvals
  for insert to authenticated with check (true);

drop policy if exists approvals_decide on public.approvals;
create policy approvals_decide on public.approvals
  for update to authenticated
  using (public.current_role_name() in ('admin','manager'))
  with check (public.current_role_name() in ('admin','manager'));

create index if not exists idx_approvals_status on public.approvals (status, created_at desc);

-- Every unassigned lead gets a pending approval, so the queue is a worklist rather than
-- a count. High priority goes straight to escalated, because a four hour clock cannot
-- wait for someone to notice it.
insert into public.approvals (lead_id, kind, status, reason)
select l.id, 'assignment',
       case when l.priority = 'high' then 'escalated' else 'pending' end,
       case when l.priority = 'high'
            then 'High priority with a 4 hour response clock and no owner'
            else 'Waiting for an owner' end
from public.leads l
where l.assigned_rep_id is null
  and l.stage not in ('won','lost')
on conflict (lead_id, kind) do nothing;

-- A newly arrived lead with no owner joins the queue automatically.
create or replace function public.queue_unassigned_lead()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.assigned_rep_id is null and new.stage not in ('won','lost') then
    insert into public.approvals (lead_id, kind, status, reason)
    values (new.id, 'assignment',
            case when new.priority = 'high' then 'escalated' else 'pending' end,
            case when new.priority = 'high'
                 then 'High priority with a 4 hour response clock and no owner'
                 else 'Waiting for an owner' end)
    on conflict (lead_id, kind) do nothing;
  elsif new.assigned_rep_id is not null then
    -- Assigning the lead settles the request that was waiting on it.
    update public.approvals
       set status = 'approved', decided_at = coalesce(decided_at, now()),
           note = coalesce(note, 'Settled automatically when the lead was assigned')
     where lead_id = new.id and kind = 'assignment' and status in ('pending','escalated');
  end if;
  return new;
end;
$$;

revoke all on function public.queue_unassigned_lead() from public, anon, authenticated;

drop trigger if exists trg_queue_unassigned on public.leads;
create trigger trg_queue_unassigned
  after insert or update of assigned_rep_id on public.leads
  for each row execute function public.queue_unassigned_lead();

select (select count(*) from public.approvals where status = 'pending')   as pending,
       (select count(*) from public.approvals where status = 'escalated') as escalated;
