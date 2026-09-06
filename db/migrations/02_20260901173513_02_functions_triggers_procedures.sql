-- Supabase migration 20260901173513  02_functions_triggers_procedures
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- FastLead CRM: functions, triggers and stored procedures.
-- Course outcomes: PL/pgSQL procedures and functions, triggers, row-level locking.

-- ------------------------------------------------------------- functions
create or replace function sla_hours_remaining(p_lead_id uuid)
returns numeric
language sql stable
as $$
  select round(extract(epoch from (c.due_at - now())) / 3600.0, 2)
  from sla_clocks c
  where c.lead_id = p_lead_id and c.stopped_at is null;
$$;
comment on function sla_hours_remaining is 'Hours left on the SLA clock. Negative means already breached.';

create or replace function lead_age_band(p_created timestamptz)
returns text
language sql immutable
as $$
  select case
    when now() - p_created < interval '1 hour'  then 'fresh'
    when now() - p_created < interval '24 hours' then 'today'
    when now() - p_created < interval '7 days'   then 'this_week'
    else 'stale'
  end;
$$;

-- current user's role, used by every RLS policy
create or replace function current_role_name()
returns user_role
language sql stable security definer set search_path = public
as $$
  select role from profiles where id = auth.uid();
$$;

-- --------------------------------------------------------------- triggers
create or replace function trg_touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

create trigger leads_touch before update on leads
for each row execute function trg_touch_updated_at();

-- stage transitions are recorded automatically, never by the application
create or replace function trg_record_stage_change() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    insert into lead_stage_history(lead_id, from_stage, to_stage, changed_by)
    values (new.id, null, new.stage, auth.uid());
  elsif new.stage is distinct from old.stage then
    insert into lead_stage_history(lead_id, from_stage, to_stage, changed_by)
    values (new.id, old.stage, new.stage, auth.uid());
  end if;
  return new;
end $$;

create trigger leads_stage_history after insert or update on leads
for each row execute function trg_record_stage_change();

-- audit log, written by the database so the application cannot forget
create or replace function trg_audit() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into audit_log(table_name, row_id, action, actor, old_data, new_data)
  values (tg_table_name,
          coalesce(new.id::text, old.id::text),
          tg_op,
          auth.uid(),
          case when tg_op in ('UPDATE','DELETE') then to_jsonb(old) end,
          case when tg_op in ('INSERT','UPDATE') then to_jsonb(new) end);
  return coalesce(new, old);
end $$;

create trigger leads_audit after insert or update or delete on leads
for each row execute function trg_audit();
create trigger assignments_audit after insert or update or delete on assignments
for each row execute function trg_audit();

-- SLA breach flagging fires on the clock, not on a UI action
create or replace function trg_flag_breach() returns trigger
language plpgsql as $$
begin
  if new.stopped_at is null and now() > new.due_at and not new.breached then
    new.breached := true;
    insert into escalations(lead_id, reason)
    values (new.lead_id, 'SLA deadline passed');
  end if;
  return new;
end $$;

create trigger sla_breach_flag before update on sla_clocks
for each row execute function trg_flag_breach();

-- ------------------------------------------------------------- procedures
-- THE CONCURRENCY SHOWPIECE.
-- Two representatives clicking "claim" on the same lead at the same moment is a
-- textbook lost update. FOR UPDATE takes a row lock so the second transaction
-- blocks until the first commits, then sees the lead is already assigned.
create or replace function assign_lead(
  p_lead_id uuid,
  p_rep_id  uuid,
  p_policy  text default 'manual',
  p_confidence numeric default null
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  v_lead   leads%rowtype;
  v_rep    representatives%rowtype;
  v_load   int;
  v_hours  int;
begin
  -- lock the lead row. Any concurrent assign_lead on this lead waits here.
  select * into v_lead from leads where id = p_lead_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'lead not found');
  end if;
  if v_lead.assigned_rep_id is not null then
    return jsonb_build_object('ok', false, 'error', 'already assigned',
                              'assigned_to', v_lead.assigned_rep_id);
  end if;

  -- lock the representative row too, so capacity cannot be oversubscribed
  select * into v_rep from representatives where id = p_rep_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'representative not found');
  end if;
  if not v_rep.is_available then
    return jsonb_build_object('ok', false, 'error', 'representative unavailable');
  end if;

  select count(*) into v_load
  from leads
  where assigned_rep_id = p_rep_id
    and stage not in ('won','lost');

  if v_load >= v_rep.capacity then
    return jsonb_build_object('ok', false, 'error', 'representative at capacity',
                              'load', v_load, 'capacity', v_rep.capacity);
  end if;

  update leads
     set assigned_rep_id = p_rep_id,
         assigned_at     = now(),
         stage           = 'assigned'
   where id = p_lead_id;

  insert into assignments(lead_id, rep_id, policy, confidence)
  values (p_lead_id, p_rep_id, p_policy, p_confidence);

  select response_hours into v_hours from sla_policies where priority = v_lead.priority;
  v_hours := coalesce(v_hours, 24);

  insert into sla_clocks(lead_id, due_at)
  values (p_lead_id, now() + make_interval(hours => v_hours))
  on conflict (lead_id) do nothing;

  return jsonb_build_object('ok', true, 'lead_id', p_lead_id, 'rep_id', p_rep_id,
                            'sla_due_in_hours', v_hours);
end $$;

create or replace function close_lead(p_lead_id uuid, p_won boolean)
returns jsonb
language plpgsql security definer set search_path = public
as $$
begin
  update leads set stage = case when p_won then 'won' else 'lost' end
   where id = p_lead_id;
  update sla_clocks set stopped_at = now()
   where lead_id = p_lead_id and stopped_at is null;
  return jsonb_build_object('ok', true, 'lead_id', p_lead_id,
                            'stage', case when p_won then 'won' else 'lost' end);
end $$;

-- CURSOR SWEEP. Walks open SLA clocks that are near their deadline and escalates
-- them BEFORE they breach. A cursor is used deliberately: this is row-at-a-time
-- work with a side effect per row, which is what cursors are for.
create or replace function sweep_sla_escalations(p_warn_hours numeric default 2)
returns int
language plpgsql security definer set search_path = public
as $$
declare
  c cursor for
    select sc.lead_id, sc.due_at
      from sla_clocks sc
     where sc.stopped_at is null
       and sc.due_at - now() < make_interval(hours => p_warn_hours::int)
     order by sc.due_at;
  r record;
  n int := 0;
begin
  open c;
  loop
    fetch c into r;
    exit when not found;
    if not exists (select 1 from escalations e
                    where e.lead_id = r.lead_id
                      and e.created_at > now() - interval '6 hours') then
      insert into escalations(lead_id, reason)
      values (r.lead_id, 'Approaching SLA deadline');
      n := n + 1;
    end if;
  end loop;
  close c;
  return n;
end $$;
