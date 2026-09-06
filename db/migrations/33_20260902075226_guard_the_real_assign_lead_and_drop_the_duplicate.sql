-- Supabase migration 20260902075226  guard_the_real_assign_lead_and_drop_the_duplicate
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The previous migration wrote the guard into a THREE argument assign_lead. The one the
-- application actually calls takes four, so Postgres created a second overload and the
-- guard was never in the code path. Dropping the duplicate and putting the rule where it
-- belongs. This is the whole reason a migration has to be verified by calling it, not by
-- reading that it applied.

drop function if exists assign_lead(uuid, uuid, numeric);

create or replace function assign_lead(
  p_lead_id uuid, p_rep_id uuid, p_policy text default 'manual', p_confidence numeric default null
) returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  v_lead  leads%rowtype;
  v_rep   representatives%rowtype;
  v_role  text;
  v_load  int;
  v_hours int;
begin
  select * into v_lead from leads where id = p_lead_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'That lead no longer exists.');
  end if;
  if v_lead.assigned_rep_id is not null then
    return jsonb_build_object('ok', false,
      'error', 'Someone already claimed this lead a moment ago.',
      'assigned_to', v_lead.assigned_rep_id);
  end if;

  select * into v_rep from representatives where id = p_rep_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'That person is not on the team any more.');
  end if;

  -- An administrator or a manager is not a sales owner. Enforced here as well as in the
  -- interface, because a direct API call does not go through the interface.
  select coalesce(p.role::text, 'rep') into v_role from profiles p where p.id = v_rep.profile_id;
  if coalesce(v_role, 'rep') <> 'rep' then
    return jsonb_build_object('ok', false,
      'error', format('%s is an %s, not a sales representative, so cannot own a lead.',
                      v_rep.name,
                      case v_role when 'admin' then 'administrator' else v_role end));
  end if;

  if not v_rep.is_available then
    return jsonb_build_object('ok', false,
      'error', format('%s is marked unavailable. Pick someone else.', v_rep.name));
  end if;

  select count(*) into v_load
    from leads where assigned_rep_id = p_rep_id and stage not in ('won','lost');

  if v_load >= v_rep.capacity then
    return jsonb_build_object('ok', false,
      'error', format('%s is carrying %s of %s leads and has no room.',
                      v_rep.name, v_load, v_rep.capacity),
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

revoke all on function assign_lead(uuid, uuid, text, numeric) from public, anon;
grant execute on function assign_lead(uuid, uuid, text, numeric) to authenticated;
