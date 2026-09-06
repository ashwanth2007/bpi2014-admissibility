-- Supabase migration 20260901180932  08_tighten_permissive_rls_policies
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Close every "RLS Policy Always True" finding.
--
-- THE MISTAKE. Eighteen policies used WITH CHECK (true) or USING (true) on write
-- operations. That is not row level security, it is row level security switched off
-- for those operations. Concretely it allowed a signed-in user to forge audit
-- entries, push their own SLA deadline into the future so they never breach, insert
-- assignment rows without going through the locking procedure, and edit or delete
-- another user's activity records.
--
-- THE PRINCIPLE APPLIED HERE.
-- A table written ONLY by a SECURITY DEFINER trigger or procedure needs NO write
-- policy for `authenticated` at all. Definer context bypasses RLS, so the trigger
-- keeps working while the REST API is closed. Anything a user genuinely writes gets
-- a predicate that ties the row to that user.

-- ============================================================================
-- 1. TABLES WRITTEN ONLY BY TRIGGERS OR PROCEDURES. Close them completely.
-- ============================================================================

-- audit_log: written by trg_audit (SECURITY DEFINER). A user must never insert.
drop policy if exists audit_insert on audit_log;

-- lead_stage_history: written by trg_record_stage_change (SECURITY DEFINER).
drop policy if exists lsh_insert on lead_stage_history;

-- assignments: written ONLY by assign_lead, which holds the row lock. Allowing a
-- direct insert defeated the entire concurrency guarantee.
drop policy if exists assign_insert on assignments;

-- sla_clocks: created by assign_lead, stopped by close_lead, flagged by
-- trg_flag_breach. A rep could previously extend their own deadline.
drop policy if exists slac_insert on sla_clocks;
drop policy if exists slac_update on sla_clocks;

-- escalations: raised by trg_flag_breach and sweep_sla_escalations.
drop policy if exists esc_insert on escalations;

-- ============================================================================
-- 2. LEADS. A user may create a lead, but only an unowned one in an early stage.
-- ============================================================================
drop policy if exists leads_insert on leads;
create policy leads_insert on leads for insert to authenticated
  with check (
    assigned_rep_id is null                       -- cannot pre-assign to someone
    and assigned_at is null
    and stage in ('new','scoring','queued')       -- cannot create an already-won lead
  );

-- ============================================================================
-- 3. PREDICTIONS. Only against a lead the caller can actually see.
-- ============================================================================
drop policy if exists pred_insert on predictions;
create policy pred_insert on predictions for insert to authenticated
  with check (exists (select 1 from leads l where l.id = lead_id));

-- ============================================================================
-- 4. ACTIVITIES. Must be attributed to the caller, on a visible lead.
-- ============================================================================
drop policy if exists act_insert on activities;
create policy act_insert on activities for insert to authenticated
  with check (
    created_by = auth.uid()                             -- cannot post as someone else
    and exists (select 1 from leads l where l.id = lead_id)
  );

-- Subtype tables: the parent activity must exist and belong to the caller. The
-- previous policies allowed any signed-in user to edit or delete any subtype row.
do $$
declare t text;
begin
  foreach t in array array['activity_calls','activity_emails','activity_notes'] loop
    execute format('drop policy if exists %1$s_select on %1$s', t);
    execute format('drop policy if exists %1$s_insert on %1$s', t);
    execute format('drop policy if exists %1$s_update on %1$s', t);
    execute format('drop policy if exists %1$s_delete on %1$s', t);

    execute format($f$
      create policy %1$s_select on %1$s for select to authenticated
      using (exists (select 1 from activities a where a.id = %1$s.id))$f$, t);

    execute format($f$
      create policy %1$s_insert on %1$s for insert to authenticated
      with check (exists (select 1 from activities a
                          where a.id = %1$s.id and a.created_by = auth.uid()))$f$, t);

    execute format($f$
      create policy %1$s_update on %1$s for update to authenticated
      using (exists (select 1 from activities a
                     where a.id = %1$s.id and a.created_by = auth.uid()))
      with check (exists (select 1 from activities a
                          where a.id = %1$s.id and a.created_by = auth.uid()))$f$, t);

    execute format($f$
      create policy %1$s_delete on %1$s for delete to authenticated
      using (exists (select 1 from activities a
                     where a.id = %1$s.id
                       and (a.created_by = auth.uid() or current_role_name() = 'admin')))$f$, t);
  end loop;
end $$;

-- ============================================================================
-- 5. MATERIALISED VIEW. It cannot carry RLS, so it must not sit in the API.
--    Replaced by a security-invoker view over the base tables, which does.
-- ============================================================================
revoke all on mv_pipeline_summary from anon, authenticated, public;

create or replace view v_pipeline_summary
with (security_invoker = true) as
select l.stage,
       count(*)                                          as lead_count,
       round(avg(l.conversion_probability)::numeric, 4)  as avg_conversion,
       round(avg(l.sla_breach_probability)::numeric, 4)  as avg_breach_risk,
       count(*) filter (where sc.breached)               as breached_count
from leads l
left join sla_clocks sc on sc.lead_id = l.id
group by l.stage;

grant select on v_pipeline_summary to authenticated;

-- ============================================================================
-- 6. REDUCE THE SECURITY DEFINER SURFACE.
--    These two are scheduled jobs, not user actions. No user needs to call them.
--    assign_lead, close_lead and set_user_role stay callable because they are user
--    actions AND each one gates on the caller internally.
--    current_role_name and my_rep_id stay callable because RLS policies invoke them.
-- ============================================================================
revoke all on function refresh_pipeline_summary() from authenticated;
revoke all on function sweep_sla_escalations(numeric) from authenticated;
