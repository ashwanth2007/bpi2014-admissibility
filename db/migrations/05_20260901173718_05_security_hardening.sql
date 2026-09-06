-- Supabase migration 20260901173718  05_security_hardening
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- FastLead CRM: security hardening.
-- Fixes every issue raised by the Supabase security advisor after migration 04.

-- ISSUE 1. Mutable search_path on five functions.
-- A function without a pinned search_path can be hijacked by a caller who creates a
-- shadowing object in an earlier schema. Pin it on every function.
alter function sla_hours_remaining(uuid)      set search_path = public;
alter function lead_age_band(timestamptz)     set search_path = public;
alter function trg_touch_updated_at()         set search_path = public;
alter function trg_record_stage_change()      set search_path = public;
alter function trg_flag_breach()              set search_path = public;

-- ISSUE 2. SECURITY DEFINER functions were callable by anonymous users over REST.
-- assign_lead and close_lead mutate operational data. An anonymous caller could have
-- assigned or closed any lead by id. Revoke from anon and from public entirely.
revoke all on function assign_lead(uuid, uuid, text, numeric)  from public, anon;
revoke all on function close_lead(uuid, boolean)               from public, anon;
revoke all on function sweep_sla_escalations(numeric)          from public, anon;
revoke all on function refresh_pipeline_summary()              from public, anon;
revoke all on function current_role_name()                     from public, anon;
revoke all on function my_rep_id()                             from public, anon;

-- Trigger functions must never be reachable over the API at all.
revoke all on function trg_audit()                from public, anon, authenticated;
revoke all on function trg_touch_updated_at()     from public, anon, authenticated;
revoke all on function trg_record_stage_change()  from public, anon, authenticated;
revoke all on function trg_flag_breach()          from public, anon, authenticated;

-- Grant back only what a signed in user legitimately needs.
grant execute on function assign_lead(uuid, uuid, text, numeric) to authenticated;
grant execute on function close_lead(uuid, boolean)              to authenticated;
grant execute on function sweep_sla_escalations(numeric)         to authenticated;
grant execute on function refresh_pipeline_summary()             to authenticated;
grant execute on function current_role_name()                    to authenticated;
grant execute on function my_rep_id()                            to authenticated;
grant execute on function sla_hours_remaining(uuid)              to authenticated;
grant execute on function lead_age_band(timestamptz)             to authenticated;

-- ISSUE 3. The materialised view was readable by anonymous users.
-- A materialised view does not honour RLS, so exposing it over the API leaks the
-- whole pipeline summary to anyone with the publishable key.
revoke all on mv_pipeline_summary from anon, public;
grant select on mv_pipeline_summary to authenticated;

-- Belt and braces: no anonymous read on any base table either.
do $$
declare t text;
begin
  foreach t in array array[
    'profiles','representatives','leads','lead_stage_history','assignments',
    'sla_policies','sla_clocks','escalations','activities','activity_calls',
    'activity_emails','activity_notes','predictions','model_registry','audit_log'
  ] loop
    execute format('revoke all on public.%I from anon', t);
  end loop;
end $$;

-- New user bootstrap. A profile row is created automatically on sign up, so the
-- application never has to remember to do it and a user can never exist without a role.
create or replace function handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, email, full_name, role)
  values (new.id,
          new.email,
          coalesce(new.raw_user_meta_data->>'full_name', ''),
          coalesce((new.raw_user_meta_data->>'role')::user_role, 'rep'))
  on conflict (id) do nothing;
  return new;
end $$;

revoke all on function handle_new_user() from public, anon, authenticated;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();
