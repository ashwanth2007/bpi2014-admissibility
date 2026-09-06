-- Supabase migration 20260902080343  api_ingest_can_record_its_own_score
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The API route had no way to write the score it computed.
--
-- It runs with an anonymous client, because the alternative is a service-role key in the
-- web tier that would bypass row level security for every table at once. Anonymous is
-- denied at the GRANT level, so `select * from leads` returned nothing and the scoring
-- block failed silently and returned an empty object. Exactly the shape of the webhook
-- outbox bug: no session, so RLS hands back nothing, and the caller sees success.
--
-- The work cannot move into the database this time, because it needs an HTTP call to the
-- model service and Postgres cannot make one. So authentication moves instead: these two
-- functions authenticate with the SAME API KEY the caller already presented, the way
-- `ingest_lead` does, and do the privileged reads and writes on its behalf.
--
-- `ingest_lead` now also returns the feature columns and the candidate list, so the route
-- makes one call out and one call back rather than reading the row it just created.

create or replace function ingest_context(p_api_key text, p_lead_id uuid)
returns jsonb
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $$
declare
  v_hash text;
  v_ok   boolean;
  v_lead leads;
begin
  v_hash := encode(extensions.digest(coalesce(p_api_key, ''), 'sha256'), 'hex');
  select true into v_ok from api_keys
   where key_hash = v_hash and revoked_at is null and 'leads:write' = any(scopes);
  if not v_ok then
    return jsonb_build_object('ok', false, 'error', 'invalid or revoked api key');
  end if;

  select * into v_lead from leads where id = p_lead_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'lead not found');
  end if;

  return jsonb_build_object(
    'ok', true,
    'lead', jsonb_build_object(
      'id', v_lead.id,
      'source', v_lead.source,
      'priority', v_lead.priority,
      'specialisation', v_lead.specialisation,
      'company', v_lead.company,
      'job_role', v_lead.job_role,
      'email', v_lead.email,
      'phone', v_lead.phone,
      'created_at', v_lead.created_at,
      'sla_breach_probability', v_lead.sla_breach_probability
    ),
    'candidates', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', r.id, 'name', r.name, 'specialisation', r.specialisation,
               'capacity', r.capacity, 'is_available', r.is_available,
               'experience_years', r.experience_years,
               'active_load', r.active_load,
               'historical_conv_rate', r.historical_conv_rate))
        from v_assignable_reps r
       where r.is_available and r.active_load < r.capacity
    ), '[]'::jsonb)
  );
end $$;

create or replace function ingest_record_score(
  p_api_key       text,
  p_lead_id       uuid,
  p_probability   numeric,
  p_version       text,
  p_contributions jsonb default '[]'::jsonb,
  p_rep_id        uuid  default null,
  p_confidence    numeric default null
) returns jsonb
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $$
declare
  v_hash   text;
  v_ok     boolean;
  v_assign jsonb := null;
begin
  v_hash := encode(extensions.digest(coalesce(p_api_key, ''), 'sha256'), 'hex');
  select true into v_ok from api_keys
   where key_hash = v_hash and revoked_at is null and 'leads:write' = any(scopes);
  if not v_ok then
    return jsonb_build_object('ok', false, 'error', 'invalid or revoked api key');
  end if;

  update leads set conversion_probability = p_probability where id = p_lead_id;

  insert into predictions (lead_id, model_name, model_version, probability, features)
  values (p_lead_id, 'model_a_conversion', p_version, p_probability,
          jsonb_build_object('contributions', p_contributions));

  insert into usage_events (kind, model_name, model_version, detail)
  values ('model_inference', 'model_a_conversion', p_version, 'api intake');

  -- assign_lead does its own capacity, availability and role checks, and returns why it
  -- refused. Whatever it says is passed straight back to the caller.
  if p_rep_id is not null then
    v_assign := assign_lead(p_lead_id, p_rep_id, 'auto_model_recommended', p_confidence);
  end if;

  return jsonb_build_object('ok', true, 'assignment', v_assign);
end $$;

revoke all on function ingest_context(text, uuid) from public;
revoke all on function ingest_record_score(text, uuid, numeric, text, jsonb, uuid, numeric) from public;
grant execute on function ingest_context(text, uuid) to anon, authenticated;
grant execute on function ingest_record_score(text, uuid, numeric, text, jsonb, uuid, numeric) to anon, authenticated;
