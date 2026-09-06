-- Supabase migration 20260902074900  only_sales_representatives_can_own_a_lead
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- An administrator is not a sales representative.
--
-- Every recommendation in the approval queue named the same person: the account with the
-- admin role happened to carry a representatives row at 0 of 45 places, and a least-loaded
-- rule picks whoever is emptiest. It was arithmetically correct and operationally absurd.
-- An administrator configures the system and decides escalations; they do not work a lead
-- through the pipeline, and a manager oversees rather than carries.
--
-- This is enforced here rather than in the interface, because the interface is not where
-- access rules belong. A hand-written API call could assign a lead to an administrator
-- before this migration. It cannot now.

create or replace view v_assignable_reps
with (security_invoker = true) as
select r.id,
       r.name,
       r.specialisation,
       r.capacity,
       r.is_available,
       r.experience_years,
       r.profile_id,
       -- Open leads right now, not a stored counter that can drift.
       (select count(*) from leads l
         where l.assigned_rep_id = r.id
           and l.stage not in ('won', 'lost'))::int as active_load,
       -- How often their closed leads actually closed won. Used to rank, and shown as the
       -- reason, so the ranking can be checked rather than trusted.
       coalesce((
         select count(*) filter (where l.stage = 'won')::numeric
              / nullif(count(*) filter (where l.stage in ('won','lost')), 0)
         from leads l where l.assigned_rep_id = r.id
       ), 0.25) as historical_conv_rate
  from representatives r
  left join profiles p on p.id = r.profile_id
 where coalesce(p.role::text, 'rep') = 'rep';

grant select on v_assignable_reps to authenticated;

-- The procedure refuses an owner who is not a sales representative, with a message a
-- person can act on rather than a policy violation.
create or replace function assign_lead(p_lead_id uuid, p_rep_id uuid, p_confidence numeric default null)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_capacity int;
  v_load     int;
  v_role     text;
  v_name     text;
begin
  select r.capacity, r.name, coalesce(p.role::text, 'rep')
    into v_capacity, v_name, v_role
    from representatives r
    left join profiles p on p.id = r.profile_id
   where r.id = p_rep_id
   for update of r;

  if not found then
    raise exception 'That person is not on the team any more. Pick someone else.';
  end if;

  if v_role <> 'rep' then
    raise exception '% is an %, not a sales representative, so cannot own a lead. Assign it to someone in the sales team.',
      v_name, case v_role when 'admin' then 'administrator' else v_role end;
  end if;

  select count(*) into v_load
    from leads
   where assigned_rep_id = p_rep_id
     and stage not in ('won', 'lost');

  if v_load >= v_capacity then
    raise exception '% is already carrying % of % leads and has no room. Pick someone else or raise their capacity.',
      v_name, v_load, v_capacity;
  end if;

  update leads
     set assigned_rep_id   = p_rep_id,
         stage             = case when stage = 'queued' then 'assigned'::lead_stage else stage end,
         assigned_at       = coalesce(assigned_at, now()),
         assignment_confidence = p_confidence
   where id = p_lead_id;
end $$;

revoke all on function assign_lead(uuid, uuid, numeric) from public, anon;
grant execute on function assign_lead(uuid, uuid, numeric) to authenticated;
