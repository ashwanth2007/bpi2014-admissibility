-- Supabase migration 20260902062603  28_member_summary_from_representatives
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- v_member_summary started from profiles, so it returned 4 rows out of 7 people: the
-- seeded representatives carry the leads and the logged hours but have no auth account,
-- and a team page that hides the people doing the work is worse than none.
--
-- The representative IS the unit of work here: leads, time entries and project
-- membership all point at representatives.id. A profile is optional and exists only once
-- someone can sign in. So the view starts from representatives and joins the profile
-- where there is one. Column order changes, so the view is dropped rather than replaced.
drop view if exists public.v_member_summary;

create view public.v_member_summary
with (security_invoker = true) as
select
  r.id            as rep_id,
  p.id            as profile_id,
  coalesce(p.full_name, r.name)            as full_name,
  p.email,
  coalesce(p.role::text, 'rep')            as role,
  p.phone,
  p.job_title,
  coalesce(p.created_at, r.created_at)     as joined_at,
  (p.id is not null)                       as can_sign_in,
  r.specialisation,
  r.capacity,
  r.is_available,
  r.experience_years,
  r.historical_conv_rate,
  l.open_leads, l.won_leads, l.lost_leads, l.pipeline_value,
  t.hours_logged, t.billable_hours, t.entries, t.last_logged,
  pm.project_count,
  s.sla_met, s.sla_breached
from public.representatives r
left join public.profiles p on p.id = r.profile_id
left join lateral (
  select
    count(*) filter (where stage not in ('won','lost'))        as open_leads,
    count(*) filter (where stage = 'won')                      as won_leads,
    count(*) filter (where stage = 'lost')                     as lost_leads,
    coalesce(sum(deal_value) filter (where stage not in ('won','lost')), 0) as pipeline_value
  from public.leads where assigned_rep_id = r.id
) l on true
left join lateral (
  select
    coalesce(round(sum(minutes)/60.0, 1), 0)                          as hours_logged,
    coalesce(round(sum(minutes) filter (where billable)/60.0, 1), 0)  as billable_hours,
    count(*)                                                          as entries,
    max(entry_date)                                                   as last_logged
  from public.time_entries where rep_id = r.id
) t on true
left join lateral (
  select count(*) as project_count from public.project_members where rep_id = r.id
) pm on true
left join lateral (
  select
    count(*) filter (where not sc.breached) as sla_met,
    count(*) filter (where sc.breached)     as sla_breached
  from public.sla_clocks sc
  join public.leads l2 on l2.id = sc.lead_id
  where l2.assigned_rep_id = r.id
) s on true;

revoke all on public.v_member_summary from anon;
grant select on public.v_member_summary to authenticated;

select count(*) as members,
       count(*) filter (where can_sign_in) as with_accounts,
       round(sum(hours_logged)) as total_hours
from public.v_member_summary;
