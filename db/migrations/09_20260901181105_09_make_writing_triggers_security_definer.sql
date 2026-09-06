-- Supabase migration 20260901181105  09_make_writing_triggers_security_definer
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Migration 08 closed the write policies on lead_stage_history and escalations,
-- which was correct: no user should insert into them directly.
--
-- But two triggers that WRITE to those tables were not SECURITY DEFINER, so they ran
-- with the caller's privileges and lost their access at the same moment. The result
-- was that creating a lead failed entirely, caught by the verification suite.
--
-- The fix is to give exactly those two triggers definer rights, which is the correct
-- design anyway: a trigger that maintains an append-only log must not depend on the
-- caller having permission to write that log.

create or replace function trg_record_stage_change() returns trigger
language plpgsql security definer set search_path = public as $$
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

create or replace function trg_flag_breach() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.stopped_at is null and now() > new.due_at and not new.breached then
    new.breached := true;
    insert into escalations(lead_id, reason)
    values (new.lead_id, 'SLA deadline passed');
  end if;
  return new;
end $$;

-- Trigger functions must never be reachable over the REST API.
revoke all on function trg_record_stage_change() from public, anon, authenticated;
revoke all on function trg_flag_breach()         from public, anon, authenticated;
