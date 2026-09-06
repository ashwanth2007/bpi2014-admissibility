-- Supabase migration 20260902054011  25_fix_project_health_join_fanout
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- BUG: v_project_health joined projects to BOTH time_entries and project_tasks and then
-- aggregated. That is a join fan-out: a project with 12 time entries and 5 tasks
-- produced 60 rows, so sum(minutes) counted every hour five times and budget_used_pct
-- came out around 350 per cent on every project. It rendered fine, which is what made
-- it dangerous.
--
-- The fix is to aggregate each side in its own scalar subquery, so neither multiplies
-- the other. count(distinct) would have masked the symptom on the task counts while
-- leaving the hours wrong.
create or replace view public.v_project_health
with (security_invoker = true) as
select
  p.id, p.name, p.status, p.health, p.due_on, p.budget_hours,
  c.name   as client_name,
  ct.title as contract_title,
  ct.value as contract_value,
  ct.currency,
  ct.billing_type,
  r.name   as owner_name,
  t.hours_logged,
  t.billable_hours,
  case when p.budget_hours > 0
       then round(t.hours_logged / p.budget_hours * 100, 0)
       else null end as budget_used_pct,
  k.task_count,
  k.tasks_done
from public.projects p
left join public.clients c         on c.id  = p.client_id
left join public.contracts ct      on ct.id = p.contract_id
left join public.representatives r on r.id  = p.owner_id
left join lateral (
  select
    coalesce(round(sum(te.minutes) / 60.0, 2), 0)                           as hours_logged,
    coalesce(round(sum(te.minutes) filter (where te.billable) / 60.0, 2), 0) as billable_hours
  from public.time_entries te
  where te.project_id = p.id
) t on true
left join lateral (
  select
    count(*)                             as task_count,
    count(*) filter (where status='done') as tasks_done
  from public.project_tasks pt
  where pt.project_id = p.id
) k on true;

revoke all on public.v_project_health from anon;
grant select on public.v_project_health to authenticated;
