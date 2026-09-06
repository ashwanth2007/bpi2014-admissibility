-- Supabase migration 20260901184230  11_rep_history_for_model_b
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Model B's strongest feature is the assigned group's historical breach rate. In this
-- CRM the representative is the assignment group, so the model service needs each
-- rep's closed volume and breached count. Doing that from the app would be an N+1 of
-- COUNT queries per recommendation, and a representative cannot read a colleague's
-- lead rows under RLS anyway.
--
-- This is therefore a SECURITY DEFINER function, not a view: it returns ONLY aggregate
-- counts, never a lead id, a name or a contact detail, so it cannot be used to read
-- around row level security. It is granted to authenticated and revoked from anon and
-- public, so an anonymous caller is refused before RLS is even consulted.
create or replace function public.rep_history()
returns table (
  rep_id uuid,
  closed_leads integer,
  breached_leads integer,
  active_load integer
)
language sql
security definer
stable
set search_path = public
as $$
  select
    r.id as rep_id,
    coalesce(count(*) filter (where l.stage in ('won','lost')), 0)::int as closed_leads,
    coalesce(count(*) filter (where sc.breached), 0)::int              as breached_leads,
    coalesce(count(*) filter (where l.stage not in ('won','lost')), 0)::int as active_load
  from public.representatives r
  left join public.leads l       on l.assigned_rep_id = r.id
  left join public.sla_clocks sc on sc.lead_id = l.id
  group by r.id;
$$;

revoke all on function public.rep_history() from public, anon;
grant execute on function public.rep_history() to authenticated;

comment on function public.rep_history() is
  'Aggregate-only per-rep history for Model B target encoding. Returns no lead-level data.';
