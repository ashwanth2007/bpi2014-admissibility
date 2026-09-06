-- Supabase migration 20260902080000  fix_the_api_ingest_path_end_to_end
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Three defects on the API ingest path, all in the same twenty lines.
--
-- 1. AUTOMATIC ASSIGNMENT COULD PICK AN ADMINISTRATOR. Both `next_round_robin_rep` and
--    the least-loaded branch selected straight from `representatives`, which includes the
--    administrator's row at zero load. Round robin handed roughly one lead in seven to
--    someone who is not a sales owner, while the manual path had already been fixed to
--    use `v_assignable_reps`.
--
-- 2. THE FUNCTION REPORTED AN ASSIGNMENT THAT MAY NOT HAVE HAPPENED. It called
--    `assign_lead` with PERFORM, which throws the result away, then returned
--    `assigned_rep_id: v_rep` regardless. If the representative filled up between the
--    pick and the write, `assign_lead` returned ok:false, the lead stayed queued, and the
--    caller was told who owned it.
--
-- 3. `model_recommended` SILENTLY DID NOTHING. Neither branch matched it, so `v_rep`
--    stayed null and the lead sat queued forever while the automations card showed the
--    strategy as On. That is the one strategy this project is about. Postgres genuinely
--    cannot reach the model service over HTTP, so the function now SAYS SO in its return
--    value and hands the decision to the caller, rather than pretending it handled it.

create or replace function next_round_robin_rep()
returns uuid
language sql
stable
security definer
set search_path = public
as $$
  select r.id
    from v_assignable_reps r
   where r.is_available
     and r.active_load < r.capacity
   order by (select max(a.assigned_at) from assignments a where a.rep_id = r.id)
              nulls first,
            r.active_load
   limit 1;
$$;

create or replace function ingest_lead(p_api_key text, p_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
  v_key    api_keys;
  v_hash   text;
  v_lead   leads;
  v_rep    uuid;
  v_auto   org_settings;
  v_result jsonb;
  v_owner  uuid := null;
  v_note   text := null;
begin
  if p_api_key is null or length(p_api_key) < 20 then
    return jsonb_build_object('ok', false, 'error', 'missing or malformed api key');
  end if;

  v_hash := encode(extensions.digest(p_api_key, 'sha256'), 'hex');

  select * into v_key from api_keys where key_hash = v_hash and revoked_at is null;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'invalid or revoked api key');
  end if;

  if not ('leads:write' = any(v_key.scopes)) then
    return jsonb_build_object('ok', false, 'error', 'key lacks the leads:write scope');
  end if;

  if coalesce(p_payload->>'full_name', '') = '' then
    return jsonb_build_object('ok', false, 'error', 'full_name is required');
  end if;

  insert into leads (
    full_name, email, phone, company, job_role,
    source, campaign, channel,
    utm_source, utm_medium, utm_campaign, utm_term, utm_content,
    referrer_url, landing_page,
    address_line, city, state_region, country, postcode, timezone,
    priority, specialisation, deal_value, currency,
    employee_count, industry, website, linkedin_url, notes, stage
  ) values (
    p_payload->>'full_name',
    nullif(p_payload->>'email',''), nullif(p_payload->>'phone',''),
    nullif(p_payload->>'company',''), nullif(p_payload->>'job_role',''),
    coalesce(nullif(p_payload->>'source',''), 'webhook'),
    nullif(p_payload->>'campaign',''), nullif(p_payload->>'channel',''),
    nullif(p_payload->>'utm_source',''), nullif(p_payload->>'utm_medium',''),
    nullif(p_payload->>'utm_campaign',''), nullif(p_payload->>'utm_term',''),
    nullif(p_payload->>'utm_content',''),
    nullif(p_payload->>'referrer_url',''), nullif(p_payload->>'landing_page',''),
    nullif(p_payload->>'address_line',''), nullif(p_payload->>'city',''),
    nullif(p_payload->>'state_region',''), nullif(p_payload->>'country',''),
    nullif(p_payload->>'postcode',''), nullif(p_payload->>'timezone',''),
    coalesce(nullif(p_payload->>'priority',''), 'medium'),
    coalesce(nullif(p_payload->>'specialisation',''), 'general'),
    (p_payload->>'deal_value')::numeric,
    coalesce(nullif(p_payload->>'currency',''), 'USD'),
    (p_payload->>'employee_count')::integer,
    nullif(p_payload->>'industry',''), nullif(p_payload->>'website',''),
    nullif(p_payload->>'linkedin_url',''), nullif(p_payload->>'notes',''),
    'queued'
  ) returning * into v_lead;

  select * into v_auto from org_settings where id;

  if v_auto.auto_assign then
    if v_auto.auto_assign_strategy = 'round_robin' then
      v_rep := next_round_robin_rep();

    elsif v_auto.auto_assign_strategy = 'least_loaded' then
      select r.id into v_rep
        from v_assignable_reps r
       where r.is_available and r.active_load < r.capacity
       order by r.active_load
       limit 1;

    elsif v_auto.auto_assign_strategy = 'model_recommended' then
      v_note := 'model_recommended is resolved by the application, which is the only tier that can reach the model service';
    end if;

    if v_rep is not null then
      v_result := assign_lead(v_lead.id, v_rep, 'auto_' || v_auto.auto_assign_strategy, 0.5);
      if coalesce((v_result->>'ok')::boolean, false) then
        v_owner := v_rep;
      else
        v_note := v_result->>'error';
      end if;
    end if;
  end if;

  update api_keys
     set last_used_at = now(), request_count = request_count + 1
   where id = v_key.id;

  insert into usage_events (kind, detail) values ('api_request', 'POST /api/v1/leads');

  select * into v_lead from leads where id = v_lead.id;

  return jsonb_build_object(
    'ok', true,
    'lead_id', v_lead.id,
    'stage', v_lead.stage,
    'assigned_rep_id', v_owner,
    'needs_model_assignment', (coalesce(v_auto.auto_assign, false)
                               and v_auto.auto_assign_strategy = 'model_recommended'
                               and v_owner is null),
    'note', v_note
  );
end;
$function$;

revoke all on function ingest_lead(text, jsonb) from public;
grant execute on function ingest_lead(text, jsonb) to anon, authenticated;
