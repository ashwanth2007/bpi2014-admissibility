-- Supabase migration 20260902045526  17_event_triggers_round_robin_realtime
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Events are emitted by triggers, not by the application, for the same reason the audit
-- log is: the application can forget, a trigger cannot. Round robin lives in the
-- database for the same reason the assignment lock does, so two simultaneous callers
-- cannot both be handed the same representative.

-- ---------------------------------------------------------------- outbox emitter
create or replace function public.emit_lead_event()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_event text;
begin
  if tg_op = 'INSERT' then
    v_event := 'lead.created';
  elsif new.stage is distinct from old.stage then
    v_event := case new.stage
                 when 'won'  then 'lead.won'
                 when 'lost' then 'lead.lost'
                 else 'lead.stage_changed'
               end;
  elsif new.assigned_rep_id is distinct from old.assigned_rep_id
        and new.assigned_rep_id is not null then
    v_event := 'lead.assigned';
  else
    return new;  -- an edit that no integration cares about
  end if;

  -- Contact details are deliberately included: the receiving system is the customer's
  -- own, authenticated by a signed secret they hold. What is NOT included is anything
  -- about other leads or other representatives.
  insert into public.event_outbox (event, payload)
  values (v_event, jsonb_build_object(
    'event', v_event,
    'occurred_at', now(),
    'lead', jsonb_build_object(
      'id', new.id, 'full_name', new.full_name, 'email', new.email,
      'phone', new.phone, 'company', new.company, 'job_role', new.job_role,
      'source', new.source, 'campaign', new.campaign,
      'utm_source', new.utm_source, 'utm_medium', new.utm_medium,
      'utm_campaign', new.utm_campaign, 'referrer_url', new.referrer_url,
      'city', new.city, 'country', new.country,
      'priority', new.priority, 'specialisation', new.specialisation,
      'stage', new.stage,
      'conversion_probability', new.conversion_probability,
      'sla_breach_probability', new.sla_breach_probability,
      'assigned_rep_id', new.assigned_rep_id,
      'deal_value', new.deal_value, 'currency', new.currency,
      'created_at', new.created_at)));
  return new;
end;
$$;

drop trigger if exists trg_emit_lead_event on public.leads;
create trigger trg_emit_lead_event
  after insert or update on public.leads
  for each row execute function public.emit_lead_event();

-- ---------------------------------------------------------------- round robin
-- The cursor advance and the read happen under one row lock on org_settings, so two
-- concurrent auto-assignments get different representatives instead of the same one.
create or replace function public.next_round_robin_rep()
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_ids  uuid[];
  v_cur  integer;
  v_pick uuid;
begin
  select array_agg(r.id order by r.created_at, r.id)
    into v_ids
  from public.representatives r
  where r.is_available
    and (select count(*) from public.leads l
         where l.assigned_rep_id = r.id and l.stage not in ('won','lost')) < r.capacity;

  if v_ids is null or array_length(v_ids, 1) = 0 then
    return null;                      -- nobody has capacity, the caller must queue it
  end if;

  update public.org_settings
     set round_robin_cursor = (round_robin_cursor + 1)
   where id
  returning round_robin_cursor into v_cur;

  v_pick := v_ids[(v_cur % array_length(v_ids, 1)) + 1];
  return v_pick;
end;
$$;

revoke all on function public.next_round_robin_rep() from public, anon;
grant execute on function public.next_round_robin_rep() to authenticated;

-- ---------------------------------------------------------------- lead search
-- One function for the leads screen so filtering, sorting and paging over thousands of
-- rows happens in the database rather than by fetching everything and filtering in the
-- browser. SECURITY INVOKER, so row level security still decides what is visible: a
-- representative filtering by "all" still only ever sees their own rows.
create or replace function public.search_leads(
  p_query      text default null,
  p_stages     text[] default null,
  p_priorities text[] default null,
  p_sources    text[] default null,
  p_rep_id     uuid default null,
  p_sort       text default 'created_at',
  p_desc       boolean default true,
  p_limit      integer default 50,
  p_offset     integer default 0
)
returns table (
  id uuid, full_name text, email text, phone text, company text, job_role text,
  city text, country text, source text, campaign text, utm_source text,
  priority text, specialisation text, stage text,
  conversion_probability numeric, sla_breach_probability numeric,
  deal_value numeric, currency text,
  assigned_rep_id uuid, rep_name text,
  sla_due_at timestamptz, created_at timestamptz, total_count bigint
)
language sql
security invoker
stable
set search_path = public
as $$
  with filtered as (
    select l.*, r.name as rep_name, sc.due_at as sla_due_at
    from public.leads l
    left join public.representatives r on r.id = l.assigned_rep_id
    left join public.sla_clocks sc on sc.lead_id = l.id
    where (p_query is null or p_query = '' or
           l.full_name ilike '%'||p_query||'%' or
           l.email     ilike '%'||p_query||'%' or
           l.company   ilike '%'||p_query||'%' or
           l.phone     ilike '%'||p_query||'%' or
           l.city      ilike '%'||p_query||'%')
      and (p_stages     is null or l.stage    = any(p_stages))
      and (p_priorities is null or l.priority = any(p_priorities))
      and (p_sources    is null or l.source   = any(p_sources))
      and (p_rep_id     is null or l.assigned_rep_id = p_rep_id)
  )
  select f.id, f.full_name, f.email, f.phone, f.company, f.job_role,
         f.city, f.country, f.source, f.campaign, f.utm_source,
         f.priority, f.specialisation, f.stage,
         f.conversion_probability, f.sla_breach_probability,
         f.deal_value, f.currency,
         f.assigned_rep_id, f.rep_name, f.sla_due_at, f.created_at,
         count(*) over () as total_count
  from filtered f
  order by
    case when p_desc then
      case p_sort
        when 'conversion' then f.conversion_probability
        when 'sla_risk'   then f.sla_breach_probability
        when 'deal_value' then f.deal_value
        else null end
    end desc nulls last,
    case when not p_desc then
      case p_sort
        when 'conversion' then f.conversion_probability
        when 'sla_risk'   then f.sla_breach_probability
        when 'deal_value' then f.deal_value
        else null end
    end asc nulls last,
    case when p_sort = 'created_at' and p_desc     then f.created_at end desc,
    case when p_sort = 'created_at' and not p_desc then f.created_at end asc,
    case when p_sort = 'name' and p_desc     then f.full_name end desc,
    case when p_sort = 'name' and not p_desc then f.full_name end asc,
    f.created_at desc
  limit greatest(1, least(p_limit, 200))
  offset greatest(0, p_offset);
$$;

revoke all on function public.search_leads(text,text[],text[],text[],uuid,text,boolean,integer,integer) from public, anon;
grant execute on function public.search_leads(text,text[],text[],text[],uuid,text,boolean,integer,integer) to authenticated;

-- ---------------------------------------------------------------- realtime
-- Adding the tables to the realtime publication is what lets the leads board update
-- live in every open browser without polling. Row level security still applies to the
-- realtime stream, so a representative is not pushed a colleague's lead.
alter publication supabase_realtime add table public.leads;
alter publication supabase_realtime add table public.escalations;
