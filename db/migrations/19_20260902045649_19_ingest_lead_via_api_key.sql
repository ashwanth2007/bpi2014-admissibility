-- Supabase migration 20260902045649  19_ingest_lead_via_api_key
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The public ingest endpoint. An external system posts a lead with an API key.
--
-- WHY THIS IS A DATABASE FUNCTION AND NOT A SERVICE-ROLE CLIENT IN THE APP.
-- This codebase has one hard architectural rule: there is no service-role key anywhere,
-- so every write passes through row level security and the access guarantees stay
-- testable. An ingest endpoint breaks that, because the caller is a machine with an API
-- key, not a signed-in user. Handing the application a service-role key to solve it
-- would put a key that bypasses ALL row level security into the web tier, which is the
-- single worst place to keep one.
--
-- Instead the check moves into the database. This function is SECURITY DEFINER and
-- callable by anon, but it authenticates the caller ITSELF against the api_keys table
-- before writing anything. An anonymous caller without a valid, unrevoked key gets a
-- rejection and no row is created. The advisor will flag this function as
-- anon-executable: that is intentional and is the entire design.
create or replace function public.ingest_lead(p_api_key text, p_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_key   public.api_keys;
  v_hash  text;
  v_lead  public.leads;
  v_rep   uuid;
  v_auto  public.org_settings;
begin
  if p_api_key is null or length(p_api_key) < 20 then
    return jsonb_build_object('ok', false, 'error', 'missing or malformed api key');
  end if;

  -- Only the digest is ever compared. The plaintext key is not stored anywhere.
  v_hash := encode(extensions.digest(p_api_key, 'sha256'), 'hex');

  select * into v_key from public.api_keys
  where key_hash = v_hash and revoked_at is null;

  if not found then
    return jsonb_build_object('ok', false, 'error', 'invalid or revoked api key');
  end if;

  if not ('leads:write' = any(v_key.scopes)) then
    return jsonb_build_object('ok', false, 'error', 'key lacks the leads:write scope');
  end if;

  if coalesce(p_payload->>'full_name', '') = '' then
    return jsonb_build_object('ok', false, 'error', 'full_name is required');
  end if;

  insert into public.leads (
    full_name, email, phone, company, job_role,
    source, campaign, channel,
    utm_source, utm_medium, utm_campaign, utm_term, utm_content,
    referrer_url, landing_page,
    address_line, city, state_region, country, postcode, timezone,
    priority, specialisation, deal_value, currency,
    employee_count, industry, website, linkedin_url, notes, stage
  ) values (
    p_payload->>'full_name',
    nullif(p_payload->>'email',''),
    nullif(p_payload->>'phone',''),
    nullif(p_payload->>'company',''),
    nullif(p_payload->>'job_role',''),
    coalesce(nullif(p_payload->>'source',''), 'webhook'),
    nullif(p_payload->>'campaign',''),
    nullif(p_payload->>'channel',''),
    nullif(p_payload->>'utm_source',''),
    nullif(p_payload->>'utm_medium',''),
    nullif(p_payload->>'utm_campaign',''),
    nullif(p_payload->>'utm_term',''),
    nullif(p_payload->>'utm_content',''),
    nullif(p_payload->>'referrer_url',''),
    nullif(p_payload->>'landing_page',''),
    nullif(p_payload->>'address_line',''),
    nullif(p_payload->>'city',''),
    nullif(p_payload->>'state_region',''),
    nullif(p_payload->>'country',''),
    nullif(p_payload->>'postcode',''),
    nullif(p_payload->>'timezone',''),
    coalesce(nullif(p_payload->>'priority',''), 'medium'),
    coalesce(nullif(p_payload->>'specialisation',''), 'general'),
    (p_payload->>'deal_value')::numeric,
    coalesce(nullif(p_payload->>'currency',''), 'USD'),
    (p_payload->>'employee_count')::integer,
    nullif(p_payload->>'industry',''),
    nullif(p_payload->>'website',''),
    nullif(p_payload->>'linkedin_url',''),
    nullif(p_payload->>'notes',''),
    'queued'
  )
  returning * into v_lead;

  -- Auto assignment, if the organisation has it switched on. Round robin and least
  -- loaded are resolved here; model_recommended is left to the application, which is
  -- the only tier that can reach the model service.
  select * into v_auto from public.org_settings where id;
  if v_auto.auto_assign then
    if v_auto.auto_assign_strategy = 'round_robin' then
      v_rep := public.next_round_robin_rep();
    elsif v_auto.auto_assign_strategy = 'least_loaded' then
      select r.id into v_rep
      from public.representatives r
      where r.is_available
      order by (select count(*) from public.leads l
                where l.assigned_rep_id = r.id and l.stage not in ('won','lost'))
      limit 1;
    end if;

    if v_rep is not null then
      perform public.assign_lead(v_lead.id, v_rep, 'auto_' || v_auto.auto_assign_strategy, 0.5);
    end if;
  end if;

  update public.api_keys
     set last_used_at = now(), request_count = request_count + 1
   where id = v_key.id;

  insert into public.usage_events (kind, detail)
  values ('api_request', 'POST /api/v1/leads');

  return jsonb_build_object('ok', true, 'lead_id', v_lead.id,
                            'stage', v_lead.stage, 'assigned_rep_id', v_rep);
end;
$$;

revoke all on function public.ingest_lead(text, jsonb) from public;
grant execute on function public.ingest_lead(text, jsonb) to anon, authenticated;

comment on function public.ingest_lead(text, jsonb) is
  'Public lead ingest. Authenticates the caller against api_keys by SHA-256 digest before writing. Intentionally anon-executable: this is the API-key path, and the alternative is a service-role key in the web tier.';

-- Creating a key: the plaintext is returned exactly once and never stored.
create or replace function public.create_api_key(p_name text, p_scopes text[] default array['leads:write'])
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_plain  text;
  v_hash   text;
  v_prefix text;
  v_id     uuid;
begin
  if public.current_role_name() <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'only an administrator may create API keys');
  end if;

  v_plain  := 'fl_live_' || encode(extensions.gen_random_bytes(24), 'hex');
  v_hash   := encode(extensions.digest(v_plain, 'sha256'), 'hex');
  v_prefix := left(v_plain, 16);

  insert into public.api_keys (name, key_prefix, key_hash, scopes, created_by)
  values (p_name, v_prefix, v_hash, p_scopes, auth.uid())
  returning id into v_id;

  -- The only moment the plaintext exists outside the caller's request.
  return jsonb_build_object('ok', true, 'id', v_id, 'key', v_plain, 'prefix', v_prefix);
end;
$$;

revoke all on function public.create_api_key(text, text[]) from public, anon;
grant execute on function public.create_api_key(text, text[]) to authenticated;
