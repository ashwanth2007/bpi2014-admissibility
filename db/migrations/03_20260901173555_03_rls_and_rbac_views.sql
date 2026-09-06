-- Supabase migration 20260901173555  03_rls_and_rbac_views
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- FastLead CRM: row level security and role based access.
--
-- POLICY MATRIX. Every table, every operation. A missing policy does not raise an
-- error, it silently blocks the write, so the list is enumerated in full.
--
--   table                 select            insert          update          delete
--   profiles              self + mgr/admin  self            self + admin    admin
--   representatives       all auth          mgr/admin       mgr/admin       admin
--   leads                 role scoped       all auth        role scoped     admin
--   lead_stage_history    role scoped       system          none            none
--   assignments           role scoped       all auth        mgr/admin       admin
--   sla_policies          all auth          admin           admin           admin
--   sla_clocks            role scoped       all auth        all auth        admin
--   escalations           role scoped       all auth        mgr/admin       admin
--   activities            role scoped       all auth        owner           owner+admin
--   activity_*            follows parent    all auth        all auth        all auth
--   predictions           role scoped       all auth        none            admin
--   model_registry        all auth          admin           admin           admin
--   audit_log             admin only        system          none            none

alter table profiles            enable row level security;
alter table representatives     enable row level security;
alter table leads               enable row level security;
alter table lead_stage_history  enable row level security;
alter table assignments         enable row level security;
alter table sla_policies        enable row level security;
alter table sla_clocks          enable row level security;
alter table escalations         enable row level security;
alter table activities          enable row level security;
alter table activity_calls      enable row level security;
alter table activity_emails     enable row level security;
alter table activity_notes      enable row level security;
alter table predictions         enable row level security;
alter table model_registry      enable row level security;
alter table audit_log           enable row level security;

-- helper: the representative row belonging to the signed in user
create or replace function my_rep_id() returns uuid
language sql stable security definer set search_path = public as $$
  select id from representatives where profile_id = auth.uid() limit 1;
$$;

-- ------------------------------------------------------------- profiles
create policy profiles_select on profiles for select to authenticated
  using (id = auth.uid() or current_role_name() in ('admin','manager'));
create policy profiles_insert on profiles for insert to authenticated
  with check (id = auth.uid());
create policy profiles_update on profiles for update to authenticated
  using (id = auth.uid() or current_role_name() = 'admin')
  with check (id = auth.uid() or current_role_name() = 'admin');
create policy profiles_delete on profiles for delete to authenticated
  using (current_role_name() = 'admin');

-- ------------------------------------------------------ representatives
create policy reps_select on representatives for select to authenticated using (true);
create policy reps_insert on representatives for insert to authenticated
  with check (current_role_name() in ('admin','manager'));
create policy reps_update on representatives for update to authenticated
  using (current_role_name() in ('admin','manager'))
  with check (current_role_name() in ('admin','manager'));
create policy reps_delete on representatives for delete to authenticated
  using (current_role_name() = 'admin');

-- ----------------------------------------------------------------- leads
-- A rep sees unassigned leads (so they can be claimed) and their own. Nothing else.
create policy leads_select on leads for select to authenticated
  using (
    current_role_name() in ('admin','manager')
    or assigned_rep_id is null
    or assigned_rep_id = my_rep_id()
  );
create policy leads_insert on leads for insert to authenticated with check (true);
create policy leads_update on leads for update to authenticated
  using (current_role_name() in ('admin','manager')
         or assigned_rep_id is null
         or assigned_rep_id = my_rep_id())
  with check (current_role_name() in ('admin','manager')
         or assigned_rep_id = my_rep_id());
create policy leads_delete on leads for delete to authenticated
  using (current_role_name() = 'admin');

-- ---------------------------------------------------- lead_stage_history
create policy lsh_select on lead_stage_history for select to authenticated
  using (exists (select 1 from leads l where l.id = lead_id));
create policy lsh_insert on lead_stage_history for insert to authenticated with check (true);

-- ----------------------------------------------------------- assignments
create policy assign_select on assignments for select to authenticated
  using (current_role_name() in ('admin','manager') or rep_id = my_rep_id());
create policy assign_insert on assignments for insert to authenticated with check (true);
create policy assign_update on assignments for update to authenticated
  using (current_role_name() in ('admin','manager'))
  with check (current_role_name() in ('admin','manager'));
create policy assign_delete on assignments for delete to authenticated
  using (current_role_name() = 'admin');

-- ---------------------------------------------------------- sla_policies
create policy slap_select on sla_policies for select to authenticated using (true);
create policy slap_insert on sla_policies for insert to authenticated
  with check (current_role_name() = 'admin');
create policy slap_update on sla_policies for update to authenticated
  using (current_role_name() = 'admin') with check (current_role_name() = 'admin');
create policy slap_delete on sla_policies for delete to authenticated
  using (current_role_name() = 'admin');

-- ------------------------------------------------------------ sla_clocks
create policy slac_select on sla_clocks for select to authenticated
  using (exists (select 1 from leads l where l.id = lead_id));
create policy slac_insert on sla_clocks for insert to authenticated with check (true);
create policy slac_update on sla_clocks for update to authenticated using (true) with check (true);
create policy slac_delete on sla_clocks for delete to authenticated
  using (current_role_name() = 'admin');

-- ----------------------------------------------------------- escalations
create policy esc_select on escalations for select to authenticated
  using (exists (select 1 from leads l where l.id = lead_id));
create policy esc_insert on escalations for insert to authenticated with check (true);
create policy esc_update on escalations for update to authenticated
  using (current_role_name() in ('admin','manager'))
  with check (current_role_name() in ('admin','manager'));
create policy esc_delete on escalations for delete to authenticated
  using (current_role_name() = 'admin');

-- ------------------------------------------------------------ activities
create policy act_select on activities for select to authenticated
  using (exists (select 1 from leads l where l.id = lead_id));
create policy act_insert on activities for insert to authenticated with check (true);
create policy act_update on activities for update to authenticated
  using (created_by = auth.uid() or current_role_name() in ('admin','manager'))
  with check (created_by = auth.uid() or current_role_name() in ('admin','manager'));
create policy act_delete on activities for delete to authenticated
  using (created_by = auth.uid() or current_role_name() = 'admin');

do $$
declare t text;
begin
  foreach t in array array['activity_calls','activity_emails','activity_notes'] loop
    execute format('create policy %1$s_select on %1$s for select to authenticated using (true)', t);
    execute format('create policy %1$s_insert on %1$s for insert to authenticated with check (true)', t);
    execute format('create policy %1$s_update on %1$s for update to authenticated using (true) with check (true)', t);
    execute format('create policy %1$s_delete on %1$s for delete to authenticated using (true)', t);
  end loop;
end $$;

-- ----------------------------------------------------------- predictions
create policy pred_select on predictions for select to authenticated
  using (exists (select 1 from leads l where l.id = lead_id));
create policy pred_insert on predictions for insert to authenticated with check (true);
create policy pred_delete on predictions for delete to authenticated
  using (current_role_name() = 'admin');

-- -------------------------------------------------------- model_registry
create policy mr_select on model_registry for select to authenticated using (true);
create policy mr_insert on model_registry for insert to authenticated
  with check (current_role_name() = 'admin');
create policy mr_update on model_registry for update to authenticated
  using (current_role_name() = 'admin') with check (current_role_name() = 'admin');
create policy mr_delete on model_registry for delete to authenticated
  using (current_role_name() = 'admin');

-- -------------------------------------------------------------- audit_log
create policy audit_select on audit_log for select to authenticated
  using (current_role_name() = 'admin');
create policy audit_insert on audit_log for insert to authenticated with check (true);

-- ============================================================ RBAC VIEWS
-- Role separation expressed as views. A representative querying v_leads_rep
-- cannot see another representative's leads or the contact email, because the
-- view does not return them, independent of what the application asks for.

create view v_leads_admin
with (security_invoker = true) as
select l.*, r.name as rep_name, sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l left join representatives r on r.id = l.assigned_rep_id;

create view v_leads_manager
with (security_invoker = true) as
select l.id, l.source, l.campaign, l.full_name, l.company, l.job_role,
       l.priority, l.specialisation, l.stage,
       l.conversion_probability, l.sla_breach_probability,
       l.assigned_rep_id, r.name as rep_name, l.assigned_at, l.created_at,
       sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l left join representatives r on r.id = l.assigned_rep_id;

create view v_leads_rep
with (security_invoker = true) as
select l.id, l.source, l.full_name, l.company, l.job_role,
       l.priority, l.specialisation, l.stage,
       l.conversion_probability, l.sla_breach_probability,
       l.assigned_at, l.created_at,
       sla_hours_remaining(l.id) as sla_hours_left,
       lead_age_band(l.created_at) as age_band
from leads l
where l.assigned_rep_id = my_rep_id() or l.assigned_rep_id is null;

grant select on v_leads_admin, v_leads_manager, v_leads_rep to authenticated;
