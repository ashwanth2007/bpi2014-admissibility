-- Supabase migration 20260902051831  22_revoke_anon_on_new_tables
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- A probe showed the tables added in migrations 14 to 20 return an empty set to an
-- anonymous caller rather than an error. That is row level security doing its job, but
-- it is one layer where `leads` has two: anon is refused at the GRANT level there,
-- before any policy is consulted. Defence in depth means a policy mistake on one of
-- these tables should still not expose it publicly.
--
-- The one deliberate exception is the ingest path, which stays reachable by anon: it
-- authenticates the caller against api_keys inside `ingest_lead` before writing, and
-- the alternative is a service-role key in the web tier.
do $$
declare
  t text;
begin
  foreach t in array array[
    'org_settings','team_invitations','api_keys','webhooks','webhook_deliveries',
    'event_outbox','usage_events','nurture_sequences','nurture_steps',
    'nurture_enrolments','meetings','offers','payments','clients','deliverables'
  ]
  loop
    execute format('revoke all on table public.%I from anon', t);
  end loop;
end $$;
