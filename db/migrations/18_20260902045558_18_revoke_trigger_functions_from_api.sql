-- Supabase migration 20260902045558  18_revoke_trigger_functions_from_api
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The security advisor flagged two NEW findings introduced by migration 14 and 17:
-- emit_lead_event() and lock_attribution() are trigger functions, but PostgREST exposes
-- every function in the public schema as an RPC endpoint, so anon could call them
-- directly. A trigger function called outside a trigger has no NEW or OLD record and
-- would error rather than do damage, but an anonymous caller should not be able to
-- reach it at all, and the same is true of the other trigger functions that predate it.
--
-- The fix is a revoke, not a rewrite: these must stay SECURITY DEFINER because they
-- write to tables the calling user cannot write to directly, which is the whole point
-- of putting the audit trail and the event outbox beyond the application's reach.
do $$
declare
  fn text;
begin
  foreach fn in array array[
    'emit_lead_event()',
    'lock_attribution()',
    'handle_new_user()'
  ]
  loop
    begin
      execute format('revoke all on function public.%s from public, anon, authenticated', fn);
    exception when undefined_function then
      raise notice 'skipped %, not present', fn;
    end;
  end loop;
end $$;

-- Belt and braces for every other trigger function in the schema: anything that takes
-- no arguments and returns `trigger` has no legitimate caller except the trigger system.
do $$
declare
  r record;
begin
  for r in
    select p.oid::regprocedure::text as sig
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prorettype = 'trigger'::regtype
  loop
    execute format('revoke all on function %s from public, anon, authenticated', r.sig);
  end loop;
end $$;
