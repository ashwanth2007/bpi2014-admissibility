-- Supabase migration 20260902062501  27_team_admin_and_member_summary
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Team administration, and one view that answers "how is this person doing".
--
-- THE PERMISSION MODEL, WRITTEN DOWN RATHER THAN IMPLIED:
--
--   admin    Everything. Change anyone's role, remove a member, manage API keys and
--            webhooks, read the audit log, decide any approval, attach anyone to any
--            project. Cannot demote or delete the protected super admin.
--   manager  Sees every lead and every project. Assigns work, decides approvals,
--            attaches people to projects, reads reports. CANNOT change a role, remove a
--            member, or touch API keys, because those are account-level powers.
--   rep      Their own leads plus unassigned ones they could claim. Logs activity and
--            time. Cannot see a colleague's leads, the audit log, or any key.
--
-- The database enforces this, not the navigation. Each function below re-checks the
-- caller's role before it writes.

-- ---------------------------------------------------------------- change a role
create or replace function public.admin_set_member_role(p_profile_id uuid, p_role text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare v_email text;
begin
  if public.current_role_name() <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'Only an administrator may change a role.');
  end if;
  if p_role not in ('admin','manager','rep') then
    return jsonb_build_object('ok', false, 'error', 'Unknown role.');
  end if;

  select email into v_email from public.profiles where id = p_profile_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'No such member.');
  end if;

  -- The protected account keeps its administrator role, whoever asks.
  if lower(v_email) = 'owner@example.invalid' and p_role <> 'admin' then
    return jsonb_build_object('ok', false,
      'error', 'This account is a permanent administrator and cannot be demoted.');
  end if;

  update public.profiles set role = p_role::public.user_role where id = p_profile_id;
  return jsonb_build_object('ok', true, 'email', v_email, 'role', p_role);
end;
$$;

revoke all on function public.admin_set_member_role(uuid, text) from public, anon;
grant execute on function public.admin_set_member_role(uuid, text) to authenticated;

-- ---------------------------------------------------------------- remove a member
-- Deactivates rather than deletes. Removing the row would orphan every lead, time entry
-- and audit line that points at them, which destroys the history the audit log exists
-- to keep.
create or replace function public.admin_deactivate_member(p_profile_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare v_email text; v_rep uuid; v_open integer;
begin
  if public.current_role_name() <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'Only an administrator may remove a member.');
  end if;

  select email into v_email from public.profiles where id = p_profile_id;
  if lower(coalesce(v_email,'')) = 'owner@example.invalid' then
    return jsonb_build_object('ok', false,
      'error', 'This account is a permanent administrator and cannot be removed.');
  end if;

  select id into v_rep from public.representatives where profile_id = p_profile_id;

  select count(*) into v_open from public.leads
  where assigned_rep_id = v_rep and stage not in ('won','lost');

  if v_open > 0 then
    return jsonb_build_object('ok', false,
      'error', format('They still own %s open leads. Reassign those first.', v_open));
  end if;

  update public.representatives set is_available = false where profile_id = p_profile_id;
  update public.profiles set role = 'rep' where id = p_profile_id;
  return jsonb_build_object('ok', true, 'email', v_email);
end;
$$;

revoke all on function public.admin_deactivate_member(uuid) from public, anon;
grant execute on function public.admin_deactivate_member(uuid) to authenticated;

-- ---------------------------------------------------------------- decide an approval
create or replace function public.decide_approval(
  p_approval_id uuid, p_decision text, p_rep_id uuid default null, p_note text default null)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare a public.approvals; r jsonb;
begin
  if public.current_role_name() not in ('admin','manager') then
    return jsonb_build_object('ok', false, 'error', 'Only a manager or an administrator may decide this.');
  end if;

  select * into a from public.approvals where id = p_approval_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'No such request.');
  end if;

  if p_decision = 'approve' then
    if p_rep_id is null then
      return jsonb_build_object('ok', false, 'error', 'Choose who it goes to.');
    end if;
    -- Routed through assign_lead so the row lock, the SLA clock and the audit entry all
    -- happen exactly as they do everywhere else.
    r := public.assign_lead(a.lead_id, p_rep_id, 'approval', 0.6);
    if coalesce((r->>'ok')::boolean, false) is not true then
      return r;
    end if;
    update public.approvals
       set status='approved', decided_by=auth.uid(), decided_at=now(), note=p_note
     where id = p_approval_id;

  elsif p_decision = 'hold' then
    update public.approvals
       set status='on_hold', decided_by=auth.uid(), decided_at=now(),
           note=coalesce(p_note,'Put on hold')
     where id = p_approval_id;

  elsif p_decision = 'reject' then
    update public.approvals
       set status='rejected', decided_by=auth.uid(), decided_at=now(),
           note=coalesce(p_note,'Rejected')
     where id = p_approval_id;
    update public.leads set stage='lost' where id = a.lead_id;

  elsif p_decision = 'escalate' then
    update public.approvals
       set status='escalated', note=coalesce(p_note,'Escalated to an administrator')
     where id = p_approval_id;
  else
    return jsonb_build_object('ok', false, 'error', 'Unknown decision.');
  end if;

  return jsonb_build_object('ok', true, 'decision', p_decision);
end;
$$;

revoke all on function public.decide_approval(uuid, text, uuid, text) from public, anon;
grant execute on function public.decide_approval(uuid, text, uuid, text) to authenticated;

-- ---------------------------------------------------------------- member summary
-- One row per person answering: what are they carrying, what have they logged, how much
-- of it bills, and are they hitting their service levels. Aggregated in separate lateral
-- subqueries so no join multiplies another, the fault that made project budgets read
-- 350 per cent in migration 25.
create or replace view public.v_member_summary
with (security_invoker = true) as
select
  p.id            as profile_id,
  p.email,
  p.full_name,
  p.role,
  p.phone,
  p.job_title,
  p.created_at    as joined_at,
  r.id            as rep_id,
  r.specialisation,
  r.capacity,
  r.is_available,
  r.experience_years,
  r.historical_conv_rate,
  l.open_leads, l.won_leads, l.lost_leads, l.pipeline_value,
  t.hours_logged, t.billable_hours, t.entries, t.last_logged,
  pm.project_count,
  s.sla_met, s.sla_breached
from public.profiles p
left join public.representatives r on r.profile_id = p.id
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
