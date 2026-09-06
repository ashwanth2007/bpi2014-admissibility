-- Supabase migration 20260901175426  06_fix_privilege_escalation_and_view_leak
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- SECURITY FIX 1: privilege escalation.
--
-- The profiles_update policy allowed a user to update their own row. Because `role`
-- lives on that row, any representative could set their own role to 'admin' and gain
-- the whole system. RLS alone cannot express "you may update this row but not this
-- column", so a trigger enforces it.

create or replace function trg_guard_role_change() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  actor_role user_role;
begin
  if new.role is distinct from old.role then
    select role into actor_role from profiles where id = auth.uid();
    -- auth.uid() is null for service-role and internal calls, which are trusted.
    if auth.uid() is not null and coalesce(actor_role, 'rep') <> 'admin' then
      raise exception 'insufficient privilege: only an administrator may change a role';
    end if;
  end if;
  return new;
end $$;

revoke all on function trg_guard_role_change() from public, anon, authenticated;

drop trigger if exists profiles_guard_role on profiles;
create trigger profiles_guard_role
  before update on profiles
  for each row execute function trg_guard_role_change();

-- Undo the escalation the test performed.
update profiles set role = 'rep' where email = 'rep@example.invalid';

-- SECURITY FIX 2: the admin view exposed contact details to any signed-in user.
--
-- A view cannot carry RLS of its own. v_leads_admin is security_invoker so the
-- underlying leads policy applies, but a representative can legitimately see
-- UNASSIGNED leads (so they can claim them), and the admin view returned the email
-- and phone columns for those rows. The view must therefore gate on role itself.

drop view if exists v_leads_admin;
create view v_leads_admin
with (security_invoker = true) as
select l.*, r.name as rep_name,
       sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l
left join representatives r on r.id = l.assigned_rep_id
where current_role_name() = 'admin';

drop view if exists v_leads_manager;
create view v_leads_manager
with (security_invoker = true) as
select l.id, l.source, l.campaign, l.full_name, l.company, l.job_role,
       l.priority, l.specialisation, l.stage,
       l.conversion_probability, l.sla_breach_probability,
       l.assigned_rep_id, r.name as rep_name, l.assigned_at, l.created_at,
       sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l
left join representatives r on r.id = l.assigned_rep_id
where current_role_name() in ('admin','manager');

grant select on v_leads_admin, v_leads_manager to authenticated;

-- SECURITY FIX 3: a representative could read contact details of unassigned leads
-- through the base table. Unassigned leads must be claimable, so the row stays
-- visible, but the contact columns are masked for representatives.
drop view if exists v_leads_rep;
create view v_leads_rep
with (security_invoker = true) as
select l.id, l.source, l.full_name, l.company, l.job_role,
       l.priority, l.specialisation, l.stage,
       -- contact details only once the lead is actually theirs
       case when l.assigned_rep_id = my_rep_id() then l.email else null end as email,
       case when l.assigned_rep_id = my_rep_id() then l.phone else null end as phone,
       l.conversion_probability, l.sla_breach_probability,
       l.assigned_at, l.created_at,
       sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l
where l.assigned_rep_id = my_rep_id() or l.assigned_rep_id is null;

grant select on v_leads_rep to authenticated;
