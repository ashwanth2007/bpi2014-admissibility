--
-- PostgreSQL database dump
--

\restrict HYS2EB41JX0ClXAxZ6uOVVras1m1NDBo2WzdaNVAUL5usD2kexRlK8RGhHQyBdb

-- Dumped from database version 17.6
-- Dumped by pg_dump version 17.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: supabase_migrations; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA supabase_migrations;


--
-- Name: user_role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.user_role AS ENUM (
    'admin',
    'manager',
    'rep'
);


--
-- Name: admin_deactivate_member(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_deactivate_member(p_profile_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare v_email text; v_rep uuid; v_open integer;
begin
  if public.current_role_name() <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'Only an administrator may remove a member.');
  end if;

  select email into v_email from public.profiles where id = p_profile_id;
  if lower(coalesce(v_email,'')) = 'owner@example.invalid' then
    return jsonb_build_object('ok', false,
      'error', 'This account is a permanent administrator and cannot be removed.');
  end if;

  select id into v_rep from public.representatives where profile_id = p_profile_id;

  select count(*) into v_open from public.leads
  where assigned_rep_id = v_rep and stage not in ('won','lost');

  if v_open > 0 then
    return jsonb_build_object('ok', false,
      'error', format('They still own %s open leads. Reassign those first.', v_open));
  end if;

  update public.representatives set is_available = false where profile_id = p_profile_id;
  update public.profiles set role = 'rep' where id = p_profile_id;
  return jsonb_build_object('ok', true, 'email', v_email);
end;
$$;


--
-- Name: admin_set_member_role(uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.admin_set_member_role(p_profile_id uuid, p_role text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare v_email text;
begin
  if public.current_role_name() <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'Only an administrator may change a role.');
  end if;
  if p_role not in ('admin','manager','rep') then
    return jsonb_build_object('ok', false, 'error', 'Unknown role.');
  end if;

  select email into v_email from public.profiles where id = p_profile_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'No such member.');
  end if;

  -- The protected account keeps its administrator role, whoever asks.
  if lower(v_email) = 'owner@example.invalid' and p_role <> 'admin' then
    return jsonb_build_object('ok', false,
      'error', 'This account is a permanent administrator and cannot be demoted.');
  end if;

  update public.profiles set role = p_role::public.user_role where id = p_profile_id;
  return jsonb_build_object('ok', true, 'email', v_email, 'role', p_role);
end;
$$;


--
-- Name: assign_lead(uuid, uuid, text, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.assign_lead(p_lead_id uuid, p_rep_id uuid, p_policy text DEFAULT 'manual'::text, p_confidence numeric DEFAULT NULL::numeric) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  v_lead  leads%rowtype;
  v_rep   representatives%rowtype;
  v_role  text;
  v_load  int;
  v_hours int;
begin
  select * into v_lead from leads where id = p_lead_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'That lead no longer exists.');
  end if;
  if v_lead.assigned_rep_id is not null then
    return jsonb_build_object('ok', false,
      'error', 'Someone already claimed this lead a moment ago.',
      'assigned_to', v_lead.assigned_rep_id);
  end if;

  select * into v_rep from representatives where id = p_rep_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'That person is not on the team any more.');
  end if;

  -- An administrator or a manager is not a sales owner. Enforced here as well as in the
  -- interface, because a direct API call does not go through the interface.
  select coalesce(p.role::text, 'rep') into v_role from profiles p where p.id = v_rep.profile_id;
  if coalesce(v_role, 'rep') <> 'rep' then
    return jsonb_build_object('ok', false,
      'error', format('%s is an %s, not a sales representative, so cannot own a lead.',
                      v_rep.name,
                      case v_role when 'admin' then 'administrator' else v_role end));
  end if;

  if not v_rep.is_available then
    return jsonb_build_object('ok', false,
      'error', format('%s is marked unavailable. Pick someone else.', v_rep.name));
  end if;

  select count(*) into v_load
    from leads where assigned_rep_id = p_rep_id and stage not in ('won','lost');

  if v_load >= v_rep.capacity then
    return jsonb_build_object('ok', false,
      'error', format('%s is carrying %s of %s leads and has no room.',
                      v_rep.name, v_load, v_rep.capacity),
      'load', v_load, 'capacity', v_rep.capacity);
  end if;

  update leads
     set assigned_rep_id = p_rep_id,
         assigned_at     = now(),
         stage           = 'assigned'
   where id = p_lead_id;

  insert into assignments(lead_id, rep_id, policy, confidence)
  values (p_lead_id, p_rep_id, p_policy, p_confidence);

  select response_hours into v_hours from sla_policies where priority = v_lead.priority;
  v_hours := coalesce(v_hours, 24);

  insert into sla_clocks(lead_id, due_at)
  values (p_lead_id, now() + make_interval(hours => v_hours))
  on conflict (lead_id) do nothing;

  return jsonb_build_object('ok', true, 'lead_id', p_lead_id, 'rep_id', p_rep_id,
                            'sla_due_in_hours', v_hours);
end $$;


--
-- Name: close_lead(uuid, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.close_lead(p_lead_id uuid, p_won boolean) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  update leads set stage = case when p_won then 'won' else 'lost' end
   where id = p_lead_id;
  update sla_clocks set stopped_at = now()
   where lead_id = p_lead_id and stopped_at is null;
  return jsonb_build_object('ok', true, 'lead_id', p_lead_id,
                            'stage', case when p_won then 'won' else 'lost' end);
end $$;


--
-- Name: create_api_key(text, text[]); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.create_api_key(p_name text, p_scopes text[] DEFAULT ARRAY['leads:write'::text]) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions'
    AS $$
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


--
-- Name: current_role_name(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.current_role_name() RETURNS public.user_role
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select role from profiles where id = auth.uid();
$$;


--
-- Name: decide_approval(uuid, text, uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.decide_approval(p_approval_id uuid, p_decision text, p_rep_id uuid DEFAULT NULL::uuid, p_note text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare a public.approvals; r jsonb;
begin
  if public.current_role_name() not in ('admin','manager') then
    return jsonb_build_object('ok', false, 'error', 'Only a manager or an administrator may decide this.');
  end if;

  select * into a from public.approvals where id = p_approval_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'No such request.');
  end if;

  if p_decision = 'approve' then
    if p_rep_id is null then
      return jsonb_build_object('ok', false, 'error', 'Choose who it goes to.');
    end if;
    -- Routed through assign_lead so the row lock, the SLA clock and the audit entry all
    -- happen exactly as they do everywhere else.
    r := public.assign_lead(a.lead_id, p_rep_id, 'approval', 0.6);
    if coalesce((r->>'ok')::boolean, false) is not true then
      return r;
    end if;
    update public.approvals
       set status='approved', decided_by=auth.uid(), decided_at=now(), note=p_note
     where id = p_approval_id;

  elsif p_decision = 'hold' then
    update public.approvals
       set status='on_hold', decided_by=auth.uid(), decided_at=now(),
           note=coalesce(p_note,'Put on hold')
     where id = p_approval_id;

  elsif p_decision = 'reject' then
    update public.approvals
       set status='rejected', decided_by=auth.uid(), decided_at=now(),
           note=coalesce(p_note,'Rejected')
     where id = p_approval_id;
    update public.leads set stage='lost' where id = a.lead_id;

  elsif p_decision = 'escalate' then
    update public.approvals
       set status='escalated', note=coalesce(p_note,'Escalated to an administrator')
     where id = p_approval_id;
  else
    return jsonb_build_object('ok', false, 'error', 'Unknown decision.');
  end if;

  return jsonb_build_object('ok', true, 'decision', p_decision);
end;
$$;


--
-- Name: deliver_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.deliver_event() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions'
    AS $$
declare
  h        public.webhooks;
  v_body   text;
  v_ts     text;
  v_sig    text;
  v_req    bigint;
  v_any    boolean := false;
begin
  v_body := new.payload::text;
  v_ts   := extract(epoch from now())::bigint::text;

  for h in
    select * from public.webhooks
    where is_active and new.event = any(events)
  loop
    -- HMAC-SHA256 over "timestamp.body" under this endpoint's own secret. The receiver
    -- recomputes it. Without a signature anyone who learns the URL can post fake leads
    -- into the customer's system, and the timestamp is what stops a replay.
    v_sig := encode(extensions.hmac(v_ts || '.' || v_body, h.secret, 'sha256'), 'hex');

    select net.http_post(
      url     := h.url,
      body    := new.payload,
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'X-FastLead-Event', new.event,
        'X-FastLead-Timestamp', v_ts,
        'X-FastLead-Signature', 'sha256=' || v_sig,
        'User-Agent', 'FastLead-Webhooks/1.0'),
      timeout_milliseconds := 5000
    ) into v_req;

    insert into public.webhook_deliveries (webhook_id, event, payload, status_code, error)
    values (h.id, new.event, new.payload, null, 'queued request ' || v_req);

    update public.webhooks
       set last_fired_at = now()
     where id = h.id;

    insert into public.usage_events (kind, detail)
    values ('webhook_delivery', new.event || ' -> ' || h.name);

    v_any := true;
  end loop;

  -- An event with no subscriber is delivered by definition. Leaving it undispatched
  -- would make the queue look permanently backed up.
  update public.event_outbox set dispatched_at = now() where id = new.id;
  return new;
end;
$$;


--
-- Name: emit_lead_event(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.emit_lead_event() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  v_event text;
begin
  if tg_op = 'INSERT' then
    v_event := 'lead.created';
  elsif new.stage is distinct from old.stage then
    v_event := case new.stage
                 when 'won'  then 'lead.won'
                 when 'lost' then 'lead.lost'
                 else 'lead.stage_changed'
               end;
  elsif new.assigned_rep_id is distinct from old.assigned_rep_id
        and new.assigned_rep_id is not null then
    v_event := 'lead.assigned';
  else
    return new;  -- an edit that no integration cares about
  end if;

  -- Contact details are deliberately included: the receiving system is the customer's
  -- own, authenticated by a signed secret they hold. What is NOT included is anything
  -- about other leads or other representatives.
  insert into public.event_outbox (event, payload)
  values (v_event, jsonb_build_object(
    'event', v_event,
    'occurred_at', now(),
    'lead', jsonb_build_object(
      'id', new.id, 'full_name', new.full_name, 'email', new.email,
      'phone', new.phone, 'company', new.company, 'job_role', new.job_role,
      'source', new.source, 'campaign', new.campaign,
      'utm_source', new.utm_source, 'utm_medium', new.utm_medium,
      'utm_campaign', new.utm_campaign, 'referrer_url', new.referrer_url,
      'city', new.city, 'country', new.country,
      'priority', new.priority, 'specialisation', new.specialisation,
      'stage', new.stage,
      'conversion_probability', new.conversion_probability,
      'sla_breach_probability', new.sla_breach_probability,
      'assigned_rep_id', new.assigned_rep_id,
      'deal_value', new.deal_value, 'currency', new.currency,
      'created_at', new.created_at)));
  return new;
end;
$$;


--
-- Name: freeze_locked_time_entry(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.freeze_locked_time_entry() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  if old.locked and (new.minutes is distinct from old.minutes
                     or new.entry_date is distinct from old.entry_date
                     or new.billable is distinct from old.billable) then
    raise exception 'This time entry is locked because it has been invoiced.';
  end if;
  return new;
end;
$$;


--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.handle_new_user() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  v_role public.user_role := 'rep';
  v_name text;
  v_inv  public.team_invitations;
begin
  select * into v_inv
  from public.team_invitations
  where lower(email) = lower(new.email) and accepted_at is null
  limit 1;

  if found then
    v_role := v_inv.role;
    update public.team_invitations set accepted_at = now() where id = v_inv.id;
  end if;

  v_name := coalesce(v_inv.full_name,
                     nullif(new.raw_user_meta_data->>'full_name',''),
                     split_part(new.email,'@',1));

  insert into public.profiles (id, email, full_name, role)
  values (new.id, new.email, v_name, v_role)
  on conflict (id) do nothing;

  -- Everyone who can work a lead needs a representative row to be assigned one.
  insert into public.representatives (profile_id, name, specialisation, capacity,
                                      is_available, experience_years, historical_conv_rate)
  values (new.id, v_name, 'general', 10, true, 1, 0.15)
  on conflict do nothing;

  return new;
end;
$$;


--
-- Name: ingest_context(text, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ingest_context(p_api_key text, p_lead_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions'
    AS $$
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


--
-- Name: ingest_lead(text, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ingest_lead(p_api_key text, p_payload jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions'
    AS $$
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
$$;


--
-- Name: ingest_record_score(text, uuid, numeric, text, jsonb, uuid, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ingest_record_score(p_api_key text, p_lead_id uuid, p_probability numeric, p_version text, p_contributions jsonb DEFAULT '[]'::jsonb, p_rep_id uuid DEFAULT NULL::uuid, p_confidence numeric DEFAULT NULL::numeric) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions'
    AS $$
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


--
-- Name: lead_age_band(timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lead_age_band(p_created timestamp with time zone) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'public'
    AS $$
  select case
    when now() - p_created < interval '1 hour'  then 'fresh'
    when now() - p_created < interval '24 hours' then 'today'
    when now() - p_created < interval '7 days'   then 'this_week'
    else 'stale'
  end;
$$;


--
-- Name: lock_attribution(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.lock_attribution() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  new.source       := old.source;
  new.utm_source   := old.utm_source;
  new.utm_medium   := old.utm_medium;
  new.utm_campaign := old.utm_campaign;
  new.referrer_url := old.referrer_url;
  new.landing_page := old.landing_page;
  return new;
end;
$$;


--
-- Name: my_rep_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.my_rep_id() RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select id from representatives where profile_id = auth.uid() limit 1;
$$;


--
-- Name: next_round_robin_rep(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.next_round_robin_rep() RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select r.id
    from v_assignable_reps r
   where r.is_available
     and r.active_load < r.capacity
   order by (select max(a.assigned_at) from assignments a where a.rep_id = r.id)
              nulls first,
            r.active_load
   limit 1;
$$;


--
-- Name: queue_unassigned_lead(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.queue_unassigned_lead() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  if new.assigned_rep_id is null and new.stage not in ('won','lost') then
    insert into public.approvals (lead_id, kind, status, reason)
    values (new.id, 'assignment',
            case when new.priority = 'high' then 'escalated' else 'pending' end,
            case when new.priority = 'high'
                 then 'High priority with a 4 hour response clock and no owner'
                 else 'Waiting for an owner' end)
    on conflict (lead_id, kind) do nothing;
  elsif new.assigned_rep_id is not null then
    -- Assigning the lead settles the request that was waiting on it.
    update public.approvals
       set status = 'approved', decided_at = coalesce(decided_at, now()),
           note = coalesce(note, 'Settled automatically when the lead was assigned')
     where lead_id = new.id and kind = 'assignment' and status in ('pending','escalated');
  end if;
  return new;
end;
$$;


--
-- Name: reconcile_deliveries(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reconcile_deliveries() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions', 'net'
    AS $$
declare
  n integer := 0;
  d record;
  r record;
begin
  for d in
    select id, error from public.webhook_deliveries
    where status_code is null and error like 'queued request %'
    order by created_at desc limit 200
  loop
    select status_code, (created - now()) as age
      into r
    from net._http_response
    where id = split_part(d.error, ' ', 3)::bigint;

    if found then
      update public.webhook_deliveries
         set status_code = r.status_code,
             error = case when r.status_code between 200 and 299 then null
                          else 'HTTP ' || coalesce(r.status_code::text, 'no response') end
       where id = d.id;
      n := n + 1;
    end if;
  end loop;

  -- Carry the latest status onto the webhook row so the list shows health at a glance.
  update public.webhooks w
     set last_status = x.status_code,
         failure_count = case when x.status_code between 200 and 299 then 0
                              else w.failure_count + 1 end
  from (
    select distinct on (webhook_id) webhook_id, status_code
    from public.webhook_deliveries
    where status_code is not null
    order by webhook_id, created_at desc
  ) x
  where x.webhook_id = w.id and w.last_status is distinct from x.status_code;

  return n;
end;
$$;


--
-- Name: refresh_pipeline_summary(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.refresh_pipeline_summary() RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  refresh materialized view concurrently mv_pipeline_summary;
$$;


--
-- Name: rep_history(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rep_history() RETURNS TABLE(rep_id uuid, closed_leads integer, breached_leads integer, active_load integer)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
  select
    r.id as rep_id,
    coalesce(count(*) filter (where l.stage in ('won','lost')), 0)::int as closed_leads,
    coalesce(count(*) filter (where sc.breached), 0)::int              as breached_leads,
    coalesce(count(*) filter (where l.stage not in ('won','lost')), 0)::int as active_load
  from public.representatives r
  left join public.leads l       on l.assigned_rep_id = r.id
  left join public.sla_clocks sc on sc.lead_id = l.id
  group by r.id;
$$;


--
-- Name: search_leads(text, text[], text[], text[], uuid, text, boolean, integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.search_leads(p_query text DEFAULT NULL::text, p_stages text[] DEFAULT NULL::text[], p_priorities text[] DEFAULT NULL::text[], p_sources text[] DEFAULT NULL::text[], p_rep_id uuid DEFAULT NULL::uuid, p_sort text DEFAULT 'created_at'::text, p_desc boolean DEFAULT true, p_limit integer DEFAULT 50, p_offset integer DEFAULT 0) RETURNS TABLE(id uuid, full_name text, email text, phone text, company text, job_role text, city text, country text, source text, campaign text, utm_source text, priority text, specialisation text, stage text, conversion_probability numeric, sla_breach_probability numeric, deal_value numeric, currency text, assigned_rep_id uuid, rep_name text, sla_due_at timestamp with time zone, created_at timestamp with time zone, total_count bigint)
    LANGUAGE sql STABLE
    SET search_path TO 'public'
    AS $$
  with filtered as (
    select l.*, r.name as rep_name, sc.due_at as sla_due_at
    from public.leads l
    left join public.representatives r on r.id = l.assigned_rep_id
    left join public.sla_clocks sc on sc.lead_id = l.id
    where (p_query is null or p_query = '' or
           l.full_name ilike '%'||p_query||'%' or
           l.email     ilike '%'||p_query||'%' or
           l.company   ilike '%'||p_query||'%' or
           l.phone     ilike '%'||p_query||'%' or
           l.city      ilike '%'||p_query||'%')
      and (p_stages     is null or l.stage    = any(p_stages))
      and (p_priorities is null or l.priority = any(p_priorities))
      and (p_sources    is null or l.source   = any(p_sources))
      and (p_rep_id     is null or l.assigned_rep_id = p_rep_id)
  )
  select f.id, f.full_name, f.email, f.phone, f.company, f.job_role,
         f.city, f.country, f.source, f.campaign, f.utm_source,
         f.priority, f.specialisation, f.stage,
         f.conversion_probability, f.sla_breach_probability,
         f.deal_value, f.currency,
         f.assigned_rep_id, f.rep_name, f.sla_due_at, f.created_at,
         count(*) over () as total_count
  from filtered f
  order by
    case when p_desc then
      case p_sort
        when 'conversion' then f.conversion_probability
        when 'sla_risk'   then f.sla_breach_probability
        when 'deal_value' then f.deal_value
        else null end
    end desc nulls last,
    case when not p_desc then
      case p_sort
        when 'conversion' then f.conversion_probability
        when 'sla_risk'   then f.sla_breach_probability
        when 'deal_value' then f.deal_value
        else null end
    end asc nulls last,
    case when p_sort = 'created_at' and p_desc     then f.created_at end desc,
    case when p_sort = 'created_at' and not p_desc then f.created_at end asc,
    case when p_sort = 'name' and p_desc     then f.full_name end desc,
    case when p_sort = 'name' and not p_desc then f.full_name end asc,
    f.created_at desc
  limit greatest(1, least(p_limit, 200))
  offset greatest(0, p_offset);
$$;


--
-- Name: set_user_role(uuid, public.user_role); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_user_role(p_user_id uuid, p_role public.user_role) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  actor_role user_role;
begin
  select role into actor_role from profiles where id = auth.uid();
  if coalesce(actor_role, 'rep') <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'only an administrator may change roles');
  end if;
  if p_user_id = auth.uid() and p_role <> 'admin' then
    return jsonb_build_object('ok', false, 'error', 'an administrator cannot demote themselves');
  end if;

  update profiles set role = p_role where id = p_user_id;
  return jsonb_build_object('ok', true, 'user_id', p_user_id, 'role', p_role);
end $$;


--
-- Name: sla_hours_remaining(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sla_hours_remaining(p_lead_id uuid) RETURNS numeric
    LANGUAGE sql STABLE
    SET search_path TO 'public'
    AS $$
  select round(extract(epoch from (c.due_at - now())) / 3600.0, 2)
  from sla_clocks c
  where c.lead_id = p_lead_id and c.stopped_at is null;
$$;


--
-- Name: sweep_sla_escalations(numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sweep_sla_escalations(p_warn_hours numeric DEFAULT 2) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  c cursor for
    select sc.lead_id, sc.due_at
      from sla_clocks sc
     where sc.stopped_at is null
       and sc.due_at - now() < make_interval(hours => p_warn_hours::int)
     order by sc.due_at;
  r record;
  n int := 0;
begin
  open c;
  loop
    fetch c into r;
    exit when not found;
    if not exists (select 1 from escalations e
                    where e.lead_id = r.lead_id
                      and e.created_at > now() - interval '6 hours') then
      insert into escalations(lead_id, reason)
      values (r.lead_id, 'Approaching SLA deadline');
      n := n + 1;
    end if;
  end loop;
  close c;
  return n;
end $$;


--
-- Name: trg_audit(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_audit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  insert into audit_log(table_name, row_id, action, actor, old_data, new_data)
  values (tg_table_name,
          coalesce(new.id::text, old.id::text),
          tg_op,
          auth.uid(),
          case when tg_op in ('UPDATE','DELETE') then to_jsonb(old) end,
          case when tg_op in ('INSERT','UPDATE') then to_jsonb(new) end);
  return coalesce(new, old);
end $$;


--
-- Name: trg_flag_breach(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_flag_breach() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  if new.stopped_at is null and now() > new.due_at and not new.breached then
    new.breached := true;
    insert into escalations(lead_id, reason)
    values (new.lead_id, 'SLA deadline passed');
  end if;
  return new;
end $$;


--
-- Name: trg_guard_role_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_guard_role_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
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


--
-- Name: trg_guard_role_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_guard_role_insert() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
declare
  actor_role user_role;
begin
  if new.role <> 'rep' then
    select role into actor_role from profiles where id = auth.uid();
    if auth.uid() is not null and coalesce(actor_role, 'rep') <> 'admin' then
      raise exception 'insufficient privilege: a new profile may only be created as rep';
    end if;
  end if;
  return new;
end $$;


--
-- Name: trg_protect_super_admin(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_protect_super_admin() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
begin
  if tg_op = 'DELETE' then
    if old.email = 'owner@example.invalid' then
      raise exception 'the permanent super admin account cannot be deleted';
    end if;
    return old;
  end if;

  if old.email = 'owner@example.invalid' and new.role <> 'admin' then
    raise exception 'the permanent super admin cannot be demoted';
  end if;
  return new;
end $$;


--
-- Name: trg_record_stage_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_record_stage_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
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


--
-- Name: trg_touch_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.trg_touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public'
    AS $$
begin
  new.updated_at := now();
  return new;
end $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: activities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    kind text NOT NULL,
    created_by uuid,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT activities_kind_check CHECK ((kind = ANY (ARRAY['call'::text, 'email'::text, 'note'::text])))
);


--
-- Name: activity_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activity_calls (
    id uuid NOT NULL,
    kind text DEFAULT 'call'::text NOT NULL,
    duration_secs integer NOT NULL,
    outcome text NOT NULL,
    CONSTRAINT activity_calls_duration_secs_check CHECK ((duration_secs >= 0)),
    CONSTRAINT activity_calls_kind_check CHECK ((kind = 'call'::text)),
    CONSTRAINT activity_calls_outcome_check CHECK ((outcome = ANY (ARRAY['connected'::text, 'no_answer'::text, 'voicemail'::text, 'busy'::text])))
);


--
-- Name: activity_emails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activity_emails (
    id uuid NOT NULL,
    kind text DEFAULT 'email'::text NOT NULL,
    subject text NOT NULL,
    opened boolean DEFAULT false NOT NULL,
    CONSTRAINT activity_emails_kind_check CHECK ((kind = 'email'::text))
);


--
-- Name: activity_notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activity_notes (
    id uuid NOT NULL,
    kind text DEFAULT 'note'::text NOT NULL,
    body text NOT NULL,
    CONSTRAINT activity_notes_kind_check CHECK ((kind = 'note'::text))
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    key_prefix text NOT NULL,
    key_hash text NOT NULL,
    scopes text[] DEFAULT '{leads:write}'::text[] NOT NULL,
    created_by uuid,
    last_used_at timestamp with time zone,
    request_count bigint DEFAULT 0 NOT NULL,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approvals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    kind text DEFAULT 'assignment'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    requested_by uuid,
    decided_by uuid,
    decided_at timestamp with time zone,
    reason text,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT approvals_kind_check CHECK ((kind = ANY (ARRAY['assignment'::text, 'discount'::text, 'escalation'::text, 'refund'::text]))),
    CONSTRAINT approvals_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'escalated'::text, 'approved'::text, 'rejected'::text, 'on_hold'::text])))
);


--
-- Name: assignments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assignments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    rep_id uuid NOT NULL,
    policy text DEFAULT 'paa_nsga2'::text NOT NULL,
    confidence numeric(5,4),
    assigned_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assignments_confidence_check CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    table_name text NOT NULL,
    row_id text NOT NULL,
    action text NOT NULL,
    actor uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    old_data jsonb,
    new_data jsonb,
    CONSTRAINT audit_log_action_check CHECK ((action = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text])))
);


--
-- Name: audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;


--
-- Name: clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clients (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid,
    name text NOT NULL,
    email text,
    phone text,
    company text,
    account_owner uuid,
    status text DEFAULT 'active'::text NOT NULL,
    contract_value numeric(12,2),
    currency text DEFAULT 'USD'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT clients_status_check CHECK ((status = ANY (ARRAY['onboarding'::text, 'active'::text, 'paused'::text, 'churned'::text])))
);


--
-- Name: contracts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.contracts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_id uuid,
    lead_id uuid,
    offer_id uuid,
    title text NOT NULL,
    reference text,
    value numeric(12,2) NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    billing_type text DEFAULT 'fixed'::text NOT NULL,
    hourly_rate numeric(10,2),
    starts_on date,
    ends_on date,
    status text DEFAULT 'draft'::text NOT NULL,
    signed_at timestamp with time zone,
    signed_by text,
    document_url text,
    auto_renew boolean DEFAULT false NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scope text,
    terms text,
    payment_terms text,
    sla_terms text,
    signatory_name text,
    signatory_email text,
    signatory_title text,
    CONSTRAINT contract_dates CHECK (((ends_on IS NULL) OR (starts_on IS NULL) OR (ends_on >= starts_on))),
    CONSTRAINT contracts_billing_type_check CHECK ((billing_type = ANY (ARRAY['fixed'::text, 'hourly'::text, 'retainer'::text, 'milestone'::text]))),
    CONSTRAINT contracts_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'sent'::text, 'signed'::text, 'active'::text, 'completed'::text, 'terminated'::text]))),
    CONSTRAINT contracts_value_check CHECK ((value >= (0)::numeric)),
    CONSTRAINT hourly_needs_rate CHECK (((billing_type <> 'hourly'::text) OR (hourly_rate IS NOT NULL)))
);


--
-- Name: deliverables; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.deliverables (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    due_at timestamp with time zone,
    owner_id uuid,
    status text DEFAULT 'not_started'::text NOT NULL,
    delivered_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT deliverables_status_check CHECK ((status = ANY (ARRAY['not_started'::text, 'in_progress'::text, 'blocked'::text, 'delivered'::text, 'accepted'::text])))
);


--
-- Name: escalations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.escalations (
    id bigint NOT NULL,
    lead_id uuid NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: escalations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.escalations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: escalations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.escalations_id_seq OWNED BY public.escalations.id;


--
-- Name: event_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_outbox (
    id bigint NOT NULL,
    event text NOT NULL,
    payload jsonb NOT NULL,
    dispatched_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: event_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.event_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: event_outbox_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.event_outbox_id_seq OWNED BY public.event_outbox.id;


--
-- Name: lead_stage_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lead_stage_history (
    id bigint NOT NULL,
    lead_id uuid NOT NULL,
    from_stage text,
    to_stage text NOT NULL,
    changed_by uuid,
    changed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lead_stage_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lead_stage_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lead_stage_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lead_stage_history_id_seq OWNED BY public.lead_stage_history.id;


--
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text NOT NULL,
    campaign text,
    channel text,
    full_name text NOT NULL,
    email text,
    phone text,
    company text,
    job_role text,
    priority text DEFAULT 'medium'::text NOT NULL,
    specialisation text DEFAULT 'general'::text NOT NULL,
    stage text DEFAULT 'new'::text NOT NULL,
    conversion_probability numeric(5,4),
    sla_breach_probability numeric(5,4),
    assigned_rep_id uuid,
    assigned_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    address_line text,
    city text,
    state_region text,
    country text,
    postcode text,
    timezone text,
    utm_source text,
    utm_medium text,
    utm_campaign text,
    utm_term text,
    utm_content text,
    referrer_url text,
    landing_page text,
    deal_value numeric(12,2),
    currency text DEFAULT 'USD'::text,
    employee_count integer,
    industry text,
    website text,
    linkedin_url text,
    notes text,
    tags text[] DEFAULT '{}'::text[],
    last_contacted_at timestamp with time zone,
    next_follow_up_at timestamp with time zone,
    CONSTRAINT assigned_needs_rep CHECK (((stage = ANY (ARRAY['new'::text, 'scoring'::text, 'queued'::text, 'lost'::text])) OR (assigned_rep_id IS NOT NULL))),
    CONSTRAINT leads_conversion_probability_check CHECK (((conversion_probability >= (0)::numeric) AND (conversion_probability <= (1)::numeric))),
    CONSTRAINT leads_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text]))),
    CONSTRAINT leads_sla_breach_probability_check CHECK (((sla_breach_probability >= (0)::numeric) AND (sla_breach_probability <= (1)::numeric))),
    CONSTRAINT leads_source_check CHECK ((source = ANY (ARRAY['web_form'::text, 'webhook'::text, 'inbound_call'::text, 'import'::text, 'referral'::text, 'manual'::text]))),
    CONSTRAINT leads_stage_check CHECK ((stage = ANY (ARRAY['new'::text, 'scoring'::text, 'queued'::text, 'assigned'::text, 'contacted'::text, 'nurturing'::text, 'meeting_booked'::text, 'offer_sent'::text, 'won'::text, 'lost'::text])))
);


--
-- Name: meetings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meetings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    rep_id uuid,
    scheduled_at timestamp with time zone NOT NULL,
    duration_mins integer DEFAULT 30 NOT NULL,
    location text,
    meeting_url text,
    status text DEFAULT 'scheduled'::text NOT NULL,
    outcome text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT meetings_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'held'::text, 'no_show'::text, 'cancelled'::text, 'rescheduled'::text])))
);


--
-- Name: model_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_registry (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_name text NOT NULL,
    version text NOT NULL,
    auc numeric(5,4),
    trained_on text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT model_registry_model_name_check CHECK ((model_name = ANY (ARRAY['model_a_conversion'::text, 'model_b_sla'::text])))
);


--
-- Name: sla_clocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sla_clocks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    due_at timestamp with time zone NOT NULL,
    stopped_at timestamp with time zone,
    breached boolean DEFAULT false NOT NULL,
    CONSTRAINT due_after_start CHECK ((due_at > started_at))
);


--
-- Name: mv_pipeline_summary; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_pipeline_summary AS
 SELECT l.stage,
    count(*) AS lead_count,
    round(avg(l.conversion_probability), 4) AS avg_conversion,
    round(avg(l.sla_breach_probability), 4) AS avg_breach_risk,
    count(*) FILTER (WHERE sc.breached) AS breached_count
   FROM (public.leads l
     LEFT JOIN public.sla_clocks sc ON ((sc.lead_id = l.id)))
  GROUP BY l.stage
  WITH NO DATA;


--
-- Name: nurture_enrolments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nurture_enrolments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    sequence_id uuid NOT NULL,
    current_step integer DEFAULT 0 NOT NULL,
    next_run_at timestamp with time zone,
    status text DEFAULT 'active'::text NOT NULL,
    exited_reason text,
    enrolled_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nurture_enrolments_status_check CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'exited'::text, 'paused'::text])))
);


--
-- Name: nurture_sequences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nurture_sequences (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    exit_on text DEFAULT 'replied'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT nurture_sequences_exit_on_check CHECK ((exit_on = ANY (ARRAY['replied'::text, 'meeting_booked'::text, 'won'::text, 'lost'::text, 'never'::text])))
);


--
-- Name: nurture_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.nurture_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sequence_id uuid NOT NULL,
    step_order integer NOT NULL,
    channel text NOT NULL,
    delay_hours integer DEFAULT 24 NOT NULL,
    subject text,
    body text,
    CONSTRAINT nurture_steps_channel_check CHECK ((channel = ANY (ARRAY['email'::text, 'call'::text, 'sms'::text, 'linkedin'::text, 'task'::text]))),
    CONSTRAINT nurture_steps_delay_hours_check CHECK ((delay_hours >= 0))
);


--
-- Name: offers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lead_id uuid NOT NULL,
    title text NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    valid_until date,
    status text DEFAULT 'draft'::text NOT NULL,
    sent_at timestamp with time zone,
    responded_at timestamp with time zone,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT offers_amount_check CHECK ((amount >= (0)::numeric)),
    CONSTRAINT offers_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'sent'::text, 'viewed'::text, 'accepted'::text, 'declined'::text, 'expired'::text])))
);


--
-- Name: org_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.org_settings (
    id boolean DEFAULT true NOT NULL,
    org_name text DEFAULT 'FastLead'::text NOT NULL,
    auto_assign boolean DEFAULT false NOT NULL,
    auto_assign_strategy text DEFAULT 'round_robin'::text NOT NULL,
    sla_high_hours integer DEFAULT 4 NOT NULL,
    sla_medium_hours integer DEFAULT 24 NOT NULL,
    sla_low_hours integer DEFAULT 72 NOT NULL,
    round_robin_cursor integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT org_settings_auto_assign_strategy_check CHECK ((auto_assign_strategy = ANY (ARRAY['round_robin'::text, 'least_loaded'::text, 'model_recommended'::text]))),
    CONSTRAINT org_settings_singleton CHECK (id),
    CONSTRAINT org_settings_sla_high_hours_check CHECK (((sla_high_hours >= 1) AND (sla_high_hours <= 720))),
    CONSTRAINT org_settings_sla_low_hours_check CHECK (((sla_low_hours >= 1) AND (sla_low_hours <= 720))),
    CONSTRAINT org_settings_sla_medium_hours_check CHECK (((sla_medium_hours >= 1) AND (sla_medium_hours <= 720)))
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    offer_id uuid,
    lead_id uuid NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    method text,
    payment_link text,
    status text DEFAULT 'pending'::text NOT NULL,
    paid_at timestamp with time zone,
    reference text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payments_amount_check CHECK ((amount >= (0)::numeric)),
    CONSTRAINT payments_method_check CHECK ((method = ANY (ARRAY['card'::text, 'bank_transfer'::text, 'upi'::text, 'paypal'::text, 'other'::text]))),
    CONSTRAINT payments_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'paid'::text, 'failed'::text, 'refunded'::text])))
);


--
-- Name: predictions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.predictions (
    id bigint NOT NULL,
    lead_id uuid NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    probability numeric(5,4) NOT NULL,
    features jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT predictions_probability_check CHECK (((probability >= (0)::numeric) AND (probability <= (1)::numeric)))
);


--
-- Name: predictions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.predictions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: predictions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.predictions_id_seq OWNED BY public.predictions.id;


--
-- Name: profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.profiles (
    id uuid NOT NULL,
    email text NOT NULL,
    full_name text DEFAULT ''::text NOT NULL,
    role public.user_role DEFAULT 'rep'::public.user_role NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phone text,
    job_title text,
    avatar_url text,
    timezone text DEFAULT 'Asia/Kolkata'::text
);


--
-- Name: project_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_members (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    rep_id uuid NOT NULL,
    role text DEFAULT 'contributor'::text NOT NULL,
    allocation_pct integer DEFAULT 100 NOT NULL,
    added_by uuid,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_members_allocation_pct_check CHECK (((allocation_pct >= 1) AND (allocation_pct <= 100))),
    CONSTRAINT project_members_role_check CHECK ((role = ANY (ARRAY['lead'::text, 'contributor'::text, 'reviewer'::text, 'observer'::text])))
);


--
-- Name: project_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    assignee_id uuid,
    status text DEFAULT 'todo'::text NOT NULL,
    priority text DEFAULT 'medium'::text NOT NULL,
    estimate_hours numeric(6,2),
    due_on date,
    completed_at timestamp with time zone,
    "position" integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_tasks_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text]))),
    CONSTRAINT project_tasks_status_check CHECK ((status = ANY (ARRAY['todo'::text, 'in_progress'::text, 'blocked'::text, 'review'::text, 'done'::text])))
);


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    client_id uuid NOT NULL,
    contract_id uuid,
    name text NOT NULL,
    description text,
    owner_id uuid,
    status text DEFAULT 'planning'::text NOT NULL,
    health text DEFAULT 'on_track'::text NOT NULL,
    starts_on date,
    due_on date,
    completed_at timestamp with time zone,
    budget_hours numeric(8,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT projects_health_check CHECK ((health = ANY (ARRAY['on_track'::text, 'at_risk'::text, 'off_track'::text]))),
    CONSTRAINT projects_status_check CHECK ((status = ANY (ARRAY['planning'::text, 'active'::text, 'on_hold'::text, 'review'::text, 'completed'::text, 'cancelled'::text])))
);


--
-- Name: representatives; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.representatives (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    profile_id uuid,
    name text NOT NULL,
    specialisation text DEFAULT 'general'::text NOT NULL,
    capacity integer DEFAULT 10 NOT NULL,
    is_available boolean DEFAULT true NOT NULL,
    experience_years numeric(4,1) DEFAULT 0 NOT NULL,
    historical_conv_rate numeric(5,4) DEFAULT 0.10 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT representatives_capacity_check CHECK (((capacity >= 1) AND (capacity <= 500))),
    CONSTRAINT representatives_experience_years_check CHECK ((experience_years >= (0)::numeric)),
    CONSTRAINT representatives_historical_conv_rate_check CHECK (((historical_conv_rate >= (0)::numeric) AND (historical_conv_rate <= (1)::numeric)))
);


--
-- Name: sla_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sla_policies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    priority text NOT NULL,
    response_hours integer NOT NULL,
    CONSTRAINT sla_policies_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text]))),
    CONSTRAINT sla_policies_response_hours_check CHECK ((response_hours > 0))
);


--
-- Name: team_invitations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.team_invitations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text NOT NULL,
    role public.user_role DEFAULT 'rep'::public.user_role NOT NULL,
    full_name text,
    invited_by uuid,
    accepted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: time_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rep_id uuid NOT NULL,
    project_id uuid,
    task_id uuid,
    client_id uuid,
    entry_date date DEFAULT CURRENT_DATE NOT NULL,
    minutes integer NOT NULL,
    billable boolean DEFAULT true NOT NULL,
    description text,
    locked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT time_entries_minutes_check CHECK (((minutes > 0) AND (minutes <= 1440)))
);


--
-- Name: usage_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_events (
    id bigint NOT NULL,
    kind text NOT NULL,
    detail text,
    model_name text,
    model_version text,
    latency_ms integer,
    actor_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT usage_events_kind_check CHECK ((kind = ANY (ARRAY['model_inference'::text, 'api_request'::text, 'webhook_delivery'::text, 'lead_created'::text, 'assignment'::text, 'escalation'::text, 'page_view'::text])))
);


--
-- Name: usage_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.usage_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: usage_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.usage_events_id_seq OWNED BY public.usage_events.id;


--
-- Name: v_assignable_reps; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_assignable_reps WITH (security_invoker='true') AS
 SELECT r.id,
    r.name,
    r.specialisation,
    r.capacity,
    r.is_available,
    r.experience_years,
    r.profile_id,
    (( SELECT count(*) AS count
           FROM public.leads l
          WHERE ((l.assigned_rep_id = r.id) AND (l.stage <> ALL (ARRAY['won'::text, 'lost'::text])))))::integer AS active_load,
    COALESCE(( SELECT ((count(*) FILTER (WHERE (l.stage = 'won'::text)))::numeric / (NULLIF(count(*) FILTER (WHERE (l.stage = ANY (ARRAY['won'::text, 'lost'::text]))), 0))::numeric)
           FROM public.leads l
          WHERE (l.assigned_rep_id = r.id)), 0.25) AS historical_conv_rate
   FROM (public.representatives r
     LEFT JOIN public.profiles p ON ((p.id = r.profile_id)))
  WHERE (COALESCE((p.role)::text, 'rep'::text) = 'rep'::text);


--
-- Name: v_leads_admin; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_leads_admin WITH (security_invoker='true') AS
 SELECT l.id,
    l.source,
    l.campaign,
    l.channel,
    l.full_name,
    l.email,
    l.phone,
    l.company,
    l.job_role,
    l.priority,
    l.specialisation,
    l.stage,
    l.conversion_probability,
    l.sla_breach_probability,
    l.assigned_rep_id,
    l.assigned_at,
    l.created_at,
    l.updated_at,
    r.name AS rep_name,
    public.sla_hours_remaining(l.id) AS sla_hours_left,
    public.lead_age_band(l.created_at) AS age_band
   FROM (public.leads l
     LEFT JOIN public.representatives r ON ((r.id = l.assigned_rep_id)))
  WHERE (public.current_role_name() = 'admin'::public.user_role);


--
-- Name: v_leads_manager; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_leads_manager WITH (security_invoker='true') AS
 SELECT l.id,
    l.source,
    l.campaign,
    l.full_name,
    l.company,
    l.job_role,
    l.priority,
    l.specialisation,
    l.stage,
    l.conversion_probability,
    l.sla_breach_probability,
    l.assigned_rep_id,
    r.name AS rep_name,
    l.assigned_at,
    l.created_at,
    public.sla_hours_remaining(l.id) AS sla_hours_left,
    public.lead_age_band(l.created_at) AS age_band
   FROM (public.leads l
     LEFT JOIN public.representatives r ON ((r.id = l.assigned_rep_id)))
  WHERE (public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]));


--
-- Name: v_leads_rep; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_leads_rep WITH (security_invoker='true') AS
 SELECT id,
    source,
    full_name,
    company,
    job_role,
    priority,
    specialisation,
    stage,
        CASE
            WHEN (assigned_rep_id = public.my_rep_id()) THEN email
            ELSE NULL::text
        END AS email,
        CASE
            WHEN (assigned_rep_id = public.my_rep_id()) THEN phone
            ELSE NULL::text
        END AS phone,
    conversion_probability,
    sla_breach_probability,
    assigned_at,
    created_at,
    public.sla_hours_remaining(id) AS sla_hours_left,
    public.lead_age_band(created_at) AS age_band
   FROM public.leads l
  WHERE ((assigned_rep_id = public.my_rep_id()) OR (assigned_rep_id IS NULL));


--
-- Name: v_member_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_member_summary WITH (security_invoker='true') AS
 SELECT r.id AS rep_id,
    p.id AS profile_id,
    COALESCE(p.full_name, r.name) AS full_name,
    p.email,
    COALESCE((p.role)::text, 'rep'::text) AS role,
    p.phone,
    p.job_title,
    COALESCE(p.created_at, r.created_at) AS joined_at,
    (p.id IS NOT NULL) AS can_sign_in,
    r.specialisation,
    r.capacity,
    r.is_available,
    r.experience_years,
    r.historical_conv_rate,
    l.open_leads,
    l.won_leads,
    l.lost_leads,
    l.pipeline_value,
    t.hours_logged,
    t.billable_hours,
    t.entries,
    t.last_logged,
    pm.project_count,
    s.sla_met,
    s.sla_breached
   FROM (((((public.representatives r
     LEFT JOIN public.profiles p ON ((p.id = r.profile_id)))
     LEFT JOIN LATERAL ( SELECT count(*) FILTER (WHERE (leads.stage <> ALL (ARRAY['won'::text, 'lost'::text]))) AS open_leads,
            count(*) FILTER (WHERE (leads.stage = 'won'::text)) AS won_leads,
            count(*) FILTER (WHERE (leads.stage = 'lost'::text)) AS lost_leads,
            COALESCE(sum(leads.deal_value) FILTER (WHERE (leads.stage <> ALL (ARRAY['won'::text, 'lost'::text]))), (0)::numeric) AS pipeline_value
           FROM public.leads
          WHERE (leads.assigned_rep_id = r.id)) l ON (true))
     LEFT JOIN LATERAL ( SELECT COALESCE(round(((sum(time_entries.minutes))::numeric / 60.0), 1), (0)::numeric) AS hours_logged,
            COALESCE(round(((sum(time_entries.minutes) FILTER (WHERE time_entries.billable))::numeric / 60.0), 1), (0)::numeric) AS billable_hours,
            count(*) AS entries,
            max(time_entries.entry_date) AS last_logged
           FROM public.time_entries
          WHERE (time_entries.rep_id = r.id)) t ON (true))
     LEFT JOIN LATERAL ( SELECT count(*) AS project_count
           FROM public.project_members
          WHERE (project_members.rep_id = r.id)) pm ON (true))
     LEFT JOIN LATERAL ( SELECT count(*) FILTER (WHERE (NOT sc.breached)) AS sla_met,
            count(*) FILTER (WHERE sc.breached) AS sla_breached
           FROM (public.sla_clocks sc
             JOIN public.leads l2 ON ((l2.id = sc.lead_id)))
          WHERE (l2.assigned_rep_id = r.id)) s ON (true));


--
-- Name: v_pipeline_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_pipeline_summary WITH (security_invoker='true') AS
 SELECT l.stage,
    count(*) AS lead_count,
    round(avg(l.conversion_probability), 4) AS avg_conversion,
    round(avg(l.sla_breach_probability), 4) AS avg_breach_risk,
    count(*) FILTER (WHERE sc.breached) AS breached_count
   FROM (public.leads l
     LEFT JOIN public.sla_clocks sc ON ((sc.lead_id = l.id)))
  GROUP BY l.stage;


--
-- Name: v_project_health; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_project_health WITH (security_invoker='true') AS
 SELECT p.id,
    p.name,
    p.status,
    p.health,
    p.due_on,
    p.budget_hours,
    c.name AS client_name,
    ct.title AS contract_title,
    ct.value AS contract_value,
    ct.currency,
    ct.billing_type,
    r.name AS owner_name,
    t.hours_logged,
    t.billable_hours,
        CASE
            WHEN (p.budget_hours > (0)::numeric) THEN round(((t.hours_logged / p.budget_hours) * (100)::numeric), 0)
            ELSE NULL::numeric
        END AS budget_used_pct,
    k.task_count,
    k.tasks_done
   FROM (((((public.projects p
     LEFT JOIN public.clients c ON ((c.id = p.client_id)))
     LEFT JOIN public.contracts ct ON ((ct.id = p.contract_id)))
     LEFT JOIN public.representatives r ON ((r.id = p.owner_id)))
     LEFT JOIN LATERAL ( SELECT COALESCE(round(((sum(te.minutes))::numeric / 60.0), 2), (0)::numeric) AS hours_logged,
            COALESCE(round(((sum(te.minutes) FILTER (WHERE te.billable))::numeric / 60.0), 2), (0)::numeric) AS billable_hours
           FROM public.time_entries te
          WHERE (te.project_id = p.id)) t ON (true))
     LEFT JOIN LATERAL ( SELECT count(*) AS task_count,
            count(*) FILTER (WHERE (pt.status = 'done'::text)) AS tasks_done
           FROM public.project_tasks pt
          WHERE (pt.project_id = p.id)) k ON (true));


--
-- Name: v_rep_workload; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_rep_workload WITH (security_invoker='true') AS
 SELECT r.id,
    r.name,
    r.capacity,
    r.specialisation,
    count(l.id) FILTER (WHERE (l.stage <> ALL (ARRAY['won'::text, 'lost'::text]))) AS active_load,
    round(((100.0 * (count(l.id) FILTER (WHERE (l.stage <> ALL (ARRAY['won'::text, 'lost'::text]))))::numeric) / (NULLIF(r.capacity, 0))::numeric), 1) AS utilisation_pct,
    ( SELECT count(*) AS count
           FROM public.leads x
          WHERE ((x.assigned_rep_id = r.id) AND (x.stage = 'won'::text))) AS won_count
   FROM (public.representatives r
     LEFT JOIN public.leads l ON ((l.assigned_rep_id = r.id)))
  GROUP BY r.id, r.name, r.capacity, r.specialisation;


--
-- Name: webhook_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_deliveries (
    id bigint NOT NULL,
    webhook_id uuid,
    event text NOT NULL,
    payload jsonb NOT NULL,
    status_code integer,
    error text,
    duration_ms integer,
    attempt integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_deliveries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.webhook_deliveries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: webhook_deliveries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.webhook_deliveries_id_seq OWNED BY public.webhook_deliveries.id;


--
-- Name: webhooks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhooks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    url text NOT NULL,
    secret text NOT NULL,
    events text[] DEFAULT '{lead.created,lead.assigned,lead.stage_changed,sla.escalated,lead.won,lead.lost}'::text[] NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by uuid,
    last_status integer,
    last_fired_at timestamp with time zone,
    failure_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT webhooks_url_check CHECK ((url ~* '^https?://'::text))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: supabase_migrations; Owner: -
--

CREATE TABLE supabase_migrations.schema_migrations (
    version text NOT NULL,
    statements text[],
    name text,
    created_by text,
    idempotency_key text,
    rollback text[]
);


--
-- Name: audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);


--
-- Name: escalations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.escalations ALTER COLUMN id SET DEFAULT nextval('public.escalations_id_seq'::regclass);


--
-- Name: event_outbox id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_outbox ALTER COLUMN id SET DEFAULT nextval('public.event_outbox_id_seq'::regclass);


--
-- Name: lead_stage_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lead_stage_history ALTER COLUMN id SET DEFAULT nextval('public.lead_stage_history_id_seq'::regclass);


--
-- Name: predictions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.predictions ALTER COLUMN id SET DEFAULT nextval('public.predictions_id_seq'::regclass);


--
-- Name: usage_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events ALTER COLUMN id SET DEFAULT nextval('public.usage_events_id_seq'::regclass);


--
-- Name: webhook_deliveries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries ALTER COLUMN id SET DEFAULT nextval('public.webhook_deliveries_id_seq'::regclass);


--
-- Name: activities activities_id_kind_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activities
    ADD CONSTRAINT activities_id_kind_key UNIQUE (id, kind);


--
-- Name: activities activities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activities
    ADD CONSTRAINT activities_pkey PRIMARY KEY (id);


--
-- Name: activity_calls activity_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_calls
    ADD CONSTRAINT activity_calls_pkey PRIMARY KEY (id);


--
-- Name: activity_emails activity_emails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_emails
    ADD CONSTRAINT activity_emails_pkey PRIMARY KEY (id);


--
-- Name: activity_notes activity_notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_notes
    ADD CONSTRAINT activity_notes_pkey PRIMARY KEY (id);


--
-- Name: api_keys api_keys_key_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: approvals approvals_lead_id_kind_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_lead_id_kind_key UNIQUE (lead_id, kind);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: assignments assignments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assignments
    ADD CONSTRAINT assignments_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: clients clients_lead_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clients
    ADD CONSTRAINT clients_lead_id_key UNIQUE (lead_id);


--
-- Name: clients clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clients
    ADD CONSTRAINT clients_pkey PRIMARY KEY (id);


--
-- Name: contracts contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contracts
    ADD CONSTRAINT contracts_pkey PRIMARY KEY (id);


--
-- Name: contracts contracts_reference_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contracts
    ADD CONSTRAINT contracts_reference_key UNIQUE (reference);


--
-- Name: deliverables deliverables_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deliverables
    ADD CONSTRAINT deliverables_pkey PRIMARY KEY (id);


--
-- Name: escalations escalations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.escalations
    ADD CONSTRAINT escalations_pkey PRIMARY KEY (id);


--
-- Name: event_outbox event_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_outbox
    ADD CONSTRAINT event_outbox_pkey PRIMARY KEY (id);


--
-- Name: lead_stage_history lead_stage_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lead_stage_history
    ADD CONSTRAINT lead_stage_history_pkey PRIMARY KEY (id);


--
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- Name: meetings meetings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_pkey PRIMARY KEY (id);


--
-- Name: model_registry model_registry_model_name_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_registry
    ADD CONSTRAINT model_registry_model_name_version_key UNIQUE (model_name, version);


--
-- Name: model_registry model_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_registry
    ADD CONSTRAINT model_registry_pkey PRIMARY KEY (id);


--
-- Name: nurture_enrolments nurture_enrolments_lead_id_sequence_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_enrolments
    ADD CONSTRAINT nurture_enrolments_lead_id_sequence_id_key UNIQUE (lead_id, sequence_id);


--
-- Name: nurture_enrolments nurture_enrolments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_enrolments
    ADD CONSTRAINT nurture_enrolments_pkey PRIMARY KEY (id);


--
-- Name: nurture_sequences nurture_sequences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_sequences
    ADD CONSTRAINT nurture_sequences_pkey PRIMARY KEY (id);


--
-- Name: nurture_steps nurture_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_steps
    ADD CONSTRAINT nurture_steps_pkey PRIMARY KEY (id);


--
-- Name: nurture_steps nurture_steps_sequence_id_step_order_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_steps
    ADD CONSTRAINT nurture_steps_sequence_id_step_order_key UNIQUE (sequence_id, step_order);


--
-- Name: offers offers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offers
    ADD CONSTRAINT offers_pkey PRIMARY KEY (id);


--
-- Name: org_settings org_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.org_settings
    ADD CONSTRAINT org_settings_pkey PRIMARY KEY (id);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (id);


--
-- Name: predictions predictions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.predictions
    ADD CONSTRAINT predictions_pkey PRIMARY KEY (id);


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);


--
-- Name: project_members project_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_pkey PRIMARY KEY (id);


--
-- Name: project_members project_members_project_id_rep_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_project_id_rep_id_key UNIQUE (project_id, rep_id);


--
-- Name: project_tasks project_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_tasks
    ADD CONSTRAINT project_tasks_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: representatives representatives_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.representatives
    ADD CONSTRAINT representatives_pkey PRIMARY KEY (id);


--
-- Name: sla_clocks sla_clocks_lead_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_clocks
    ADD CONSTRAINT sla_clocks_lead_id_key UNIQUE (lead_id);


--
-- Name: sla_clocks sla_clocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_clocks
    ADD CONSTRAINT sla_clocks_pkey PRIMARY KEY (id);


--
-- Name: sla_policies sla_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_pkey PRIMARY KEY (id);


--
-- Name: sla_policies sla_policies_priority_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_priority_key UNIQUE (priority);


--
-- Name: team_invitations team_invitations_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_invitations
    ADD CONSTRAINT team_invitations_email_key UNIQUE (email);


--
-- Name: team_invitations team_invitations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_invitations
    ADD CONSTRAINT team_invitations_pkey PRIMARY KEY (id);


--
-- Name: time_entries time_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_pkey PRIMARY KEY (id);


--
-- Name: usage_events usage_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events
    ADD CONSTRAINT usage_events_pkey PRIMARY KEY (id);


--
-- Name: webhook_deliveries webhook_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY (id);


--
-- Name: webhooks webhooks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhooks
    ADD CONSTRAINT webhooks_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_idempotency_key_key; Type: CONSTRAINT; Schema: supabase_migrations; Owner: -
--

ALTER TABLE ONLY supabase_migrations.schema_migrations
    ADD CONSTRAINT schema_migrations_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: supabase_migrations; Owner: -
--

ALTER TABLE ONLY supabase_migrations.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: activities_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX activities_lead_idx ON public.activities USING btree (lead_id, occurred_at DESC);


--
-- Name: assignments_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assignments_lead_idx ON public.assignments USING btree (lead_id);


--
-- Name: audit_table_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX audit_table_idx ON public.audit_log USING btree (table_name, changed_at DESC);


--
-- Name: idx_api_keys_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_api_keys_hash ON public.api_keys USING btree (key_hash) WHERE (revoked_at IS NULL);


--
-- Name: idx_approvals_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approvals_status ON public.approvals USING btree (status, created_at DESC);


--
-- Name: idx_contracts_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_contracts_client ON public.contracts USING btree (client_id, status);


--
-- Name: idx_deliverables_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deliverables_client ON public.deliverables USING btree (client_id, status);


--
-- Name: idx_deliveries_webhook; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_deliveries_webhook ON public.webhook_deliveries USING btree (webhook_id, created_at DESC);


--
-- Name: idx_enrolments_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enrolments_due ON public.nurture_enrolments USING btree (next_run_at) WHERE (status = 'active'::text);


--
-- Name: idx_invitations_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invitations_email ON public.team_invitations USING btree (lower(email));


--
-- Name: idx_leads_conv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_conv ON public.leads USING btree (conversion_probability DESC NULLS LAST);


--
-- Name: idx_leads_priority; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_priority ON public.leads USING btree (priority);


--
-- Name: idx_leads_rep_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_rep_stage ON public.leads USING btree (assigned_rep_id, stage);


--
-- Name: idx_leads_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_search ON public.leads USING gin (to_tsvector('simple'::regconfig, ((((((((COALESCE(full_name, ''::text) || ' '::text) || COALESCE(email, ''::text)) || ' '::text) || COALESCE(company, ''::text)) || ' '::text) || COALESCE(phone, ''::text)) || ' '::text) || COALESCE(city, ''::text))));


--
-- Name: idx_leads_sla_risk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_sla_risk ON public.leads USING btree (sla_breach_probability DESC NULLS LAST);


--
-- Name: idx_leads_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_source ON public.leads USING btree (source);


--
-- Name: idx_leads_stage_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_stage_created ON public.leads USING btree (stage, created_at DESC);


--
-- Name: idx_leads_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_tags ON public.leads USING gin (tags);


--
-- Name: idx_meetings_lead; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_meetings_lead ON public.meetings USING btree (lead_id, scheduled_at DESC);


--
-- Name: idx_offers_lead; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_offers_lead ON public.offers USING btree (lead_id, created_at DESC);


--
-- Name: idx_outbox_undispatched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_outbox_undispatched ON public.event_outbox USING btree (created_at) WHERE (dispatched_at IS NULL);


--
-- Name: idx_payments_lead; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payments_lead ON public.payments USING btree (lead_id, created_at DESC);


--
-- Name: idx_project_members_rep; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_members_rep ON public.project_members USING btree (rep_id);


--
-- Name: idx_projects_client; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_client ON public.projects USING btree (client_id, status);


--
-- Name: idx_tasks_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_project ON public.project_tasks USING btree (project_id, status, "position");


--
-- Name: idx_time_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_time_project ON public.time_entries USING btree (project_id, entry_date DESC);


--
-- Name: idx_time_rep_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_time_rep_date ON public.time_entries USING btree (rep_id, entry_date DESC);


--
-- Name: idx_usage_kind_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_kind_time ON public.usage_events USING btree (kind, created_at DESC);


--
-- Name: idx_usage_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usage_model ON public.usage_events USING btree (model_name, created_at DESC);


--
-- Name: leads_priority_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_priority_queue_idx ON public.leads USING btree (stage, priority, conversion_probability DESC) WHERE (stage = ANY (ARRAY['queued'::text, 'new'::text]));


--
-- Name: leads_queue_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_queue_idx ON public.leads USING btree (stage, conversion_probability DESC);


--
-- Name: leads_stage_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX leads_stage_idx ON public.leads USING btree (stage);


--
-- Name: lsh_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX lsh_lead_idx ON public.lead_stage_history USING btree (lead_id, changed_at DESC);


--
-- Name: mv_pipeline_summary_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_pipeline_summary_stage ON public.mv_pipeline_summary USING btree (stage);


--
-- Name: predictions_lead_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX predictions_lead_idx ON public.predictions USING btree (lead_id, created_at DESC);


--
-- Name: sla_open_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sla_open_idx ON public.sla_clocks USING btree (due_at) WHERE (stopped_at IS NULL);


--
-- Name: assignments assignments_audit; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER assignments_audit AFTER INSERT OR DELETE OR UPDATE ON public.assignments FOR EACH ROW EXECUTE FUNCTION public.trg_audit();


--
-- Name: leads leads_audit; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER leads_audit AFTER INSERT OR DELETE OR UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.trg_audit();


--
-- Name: leads leads_stage_history; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER leads_stage_history AFTER INSERT OR UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.trg_record_stage_change();


--
-- Name: leads leads_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER leads_touch BEFORE UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.trg_touch_updated_at();


--
-- Name: profiles profiles_guard_role; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_guard_role BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.trg_guard_role_change();


--
-- Name: profiles profiles_guard_role_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_guard_role_insert BEFORE INSERT ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.trg_guard_role_insert();


--
-- Name: profiles profiles_protect_super_admin_del; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_protect_super_admin_del BEFORE DELETE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.trg_protect_super_admin();


--
-- Name: profiles profiles_protect_super_admin_upd; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER profiles_protect_super_admin_upd BEFORE UPDATE ON public.profiles FOR EACH ROW EXECUTE FUNCTION public.trg_protect_super_admin();


--
-- Name: sla_clocks sla_breach_flag; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER sla_breach_flag BEFORE UPDATE ON public.sla_clocks FOR EACH ROW EXECUTE FUNCTION public.trg_flag_breach();


--
-- Name: event_outbox trg_deliver_event; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_deliver_event AFTER INSERT ON public.event_outbox FOR EACH ROW EXECUTE FUNCTION public.deliver_event();


--
-- Name: leads trg_emit_lead_event; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_emit_lead_event AFTER INSERT OR UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.emit_lead_event();


--
-- Name: time_entries trg_freeze_time_entry; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_freeze_time_entry BEFORE UPDATE ON public.time_entries FOR EACH ROW EXECUTE FUNCTION public.freeze_locked_time_entry();


--
-- Name: leads trg_lock_attribution; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_lock_attribution BEFORE UPDATE ON public.leads FOR EACH ROW EXECUTE FUNCTION public.lock_attribution();


--
-- Name: leads trg_queue_unassigned; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_queue_unassigned AFTER INSERT OR UPDATE OF assigned_rep_id ON public.leads FOR EACH ROW EXECUTE FUNCTION public.queue_unassigned_lead();


--
-- Name: activities activities_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activities
    ADD CONSTRAINT activities_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.profiles(id) ON DELETE SET NULL;


--
-- Name: activities activities_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activities
    ADD CONSTRAINT activities_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: activity_calls activity_calls_id_kind_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_calls
    ADD CONSTRAINT activity_calls_id_kind_fkey FOREIGN KEY (id, kind) REFERENCES public.activities(id, kind) ON DELETE CASCADE;


--
-- Name: activity_emails activity_emails_id_kind_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_emails
    ADD CONSTRAINT activity_emails_id_kind_fkey FOREIGN KEY (id, kind) REFERENCES public.activities(id, kind) ON DELETE CASCADE;


--
-- Name: activity_notes activity_notes_id_kind_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_notes
    ADD CONSTRAINT activity_notes_id_kind_fkey FOREIGN KEY (id, kind) REFERENCES public.activities(id, kind) ON DELETE CASCADE;


--
-- Name: api_keys api_keys_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: approvals approvals_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: approvals approvals_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: approvals approvals_requested_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: assignments assignments_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assignments
    ADD CONSTRAINT assignments_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: assignments assignments_rep_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assignments
    ADD CONSTRAINT assignments_rep_id_fkey FOREIGN KEY (rep_id) REFERENCES public.representatives(id) ON DELETE CASCADE;


--
-- Name: clients clients_account_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clients
    ADD CONSTRAINT clients_account_owner_fkey FOREIGN KEY (account_owner) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: clients clients_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clients
    ADD CONSTRAINT clients_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: contracts contracts_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contracts
    ADD CONSTRAINT contracts_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.clients(id) ON DELETE CASCADE;


--
-- Name: contracts contracts_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contracts
    ADD CONSTRAINT contracts_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE SET NULL;


--
-- Name: contracts contracts_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.contracts
    ADD CONSTRAINT contracts_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.offers(id) ON DELETE SET NULL;


--
-- Name: deliverables deliverables_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deliverables
    ADD CONSTRAINT deliverables_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.clients(id) ON DELETE CASCADE;


--
-- Name: deliverables deliverables_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.deliverables
    ADD CONSTRAINT deliverables_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: escalations escalations_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.escalations
    ADD CONSTRAINT escalations_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: lead_stage_history lead_stage_history_changed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lead_stage_history
    ADD CONSTRAINT lead_stage_history_changed_by_fkey FOREIGN KEY (changed_by) REFERENCES public.profiles(id) ON DELETE SET NULL;


--
-- Name: lead_stage_history lead_stage_history_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lead_stage_history
    ADD CONSTRAINT lead_stage_history_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: leads leads_assigned_rep_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_assigned_rep_id_fkey FOREIGN KEY (assigned_rep_id) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: meetings meetings_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: meetings meetings_rep_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT meetings_rep_id_fkey FOREIGN KEY (rep_id) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: nurture_enrolments nurture_enrolments_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_enrolments
    ADD CONSTRAINT nurture_enrolments_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: nurture_enrolments nurture_enrolments_sequence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_enrolments
    ADD CONSTRAINT nurture_enrolments_sequence_id_fkey FOREIGN KEY (sequence_id) REFERENCES public.nurture_sequences(id) ON DELETE CASCADE;


--
-- Name: nurture_steps nurture_steps_sequence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.nurture_steps
    ADD CONSTRAINT nurture_steps_sequence_id_fkey FOREIGN KEY (sequence_id) REFERENCES public.nurture_sequences(id) ON DELETE CASCADE;


--
-- Name: offers offers_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offers
    ADD CONSTRAINT offers_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: payments payments_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: payments payments_offer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_offer_id_fkey FOREIGN KEY (offer_id) REFERENCES public.offers(id) ON DELETE SET NULL;


--
-- Name: predictions predictions_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.predictions
    ADD CONSTRAINT predictions_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: profiles profiles_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;


--
-- Name: project_members project_members_added_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_added_by_fkey FOREIGN KEY (added_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: project_members project_members_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_members project_members_rep_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_members
    ADD CONSTRAINT project_members_rep_id_fkey FOREIGN KEY (rep_id) REFERENCES public.representatives(id) ON DELETE CASCADE;


--
-- Name: project_tasks project_tasks_assignee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_tasks
    ADD CONSTRAINT project_tasks_assignee_id_fkey FOREIGN KEY (assignee_id) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: project_tasks project_tasks_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_tasks
    ADD CONSTRAINT project_tasks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: projects projects_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.clients(id) ON DELETE CASCADE;


--
-- Name: projects projects_contract_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES public.contracts(id) ON DELETE SET NULL;


--
-- Name: projects projects_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.representatives(id) ON DELETE SET NULL;


--
-- Name: representatives representatives_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.representatives
    ADD CONSTRAINT representatives_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.profiles(id) ON DELETE SET NULL;


--
-- Name: sla_clocks sla_clocks_lead_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_clocks
    ADD CONSTRAINT sla_clocks_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES public.leads(id) ON DELETE CASCADE;


--
-- Name: team_invitations team_invitations_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.team_invitations
    ADD CONSTRAINT team_invitations_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: time_entries time_entries_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_client_id_fkey FOREIGN KEY (client_id) REFERENCES public.clients(id) ON DELETE SET NULL;


--
-- Name: time_entries time_entries_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: time_entries time_entries_rep_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_rep_id_fkey FOREIGN KEY (rep_id) REFERENCES public.representatives(id) ON DELETE CASCADE;


--
-- Name: time_entries time_entries_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.project_tasks(id) ON DELETE SET NULL;


--
-- Name: usage_events usage_events_actor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_events
    ADD CONSTRAINT usage_events_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: webhook_deliveries webhook_deliveries_webhook_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_webhook_id_fkey FOREIGN KEY (webhook_id) REFERENCES public.webhooks(id) ON DELETE CASCADE;


--
-- Name: webhooks webhooks_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhooks
    ADD CONSTRAINT webhooks_created_by_fkey FOREIGN KEY (created_by) REFERENCES auth.users(id) ON DELETE SET NULL;


--
-- Name: activities act_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY act_delete ON public.activities FOR DELETE TO authenticated USING (((created_by = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role)));


--
-- Name: activities act_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY act_insert ON public.activities FOR INSERT TO authenticated WITH CHECK (((created_by = auth.uid()) AND (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = activities.lead_id)))));


--
-- Name: activities act_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY act_select ON public.activities FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = activities.lead_id))));


--
-- Name: activities act_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY act_update ON public.activities FOR UPDATE TO authenticated USING (((created_by = auth.uid()) OR (public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])))) WITH CHECK (((created_by = auth.uid()) OR (public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))));


--
-- Name: activities; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.activities ENABLE ROW LEVEL SECURITY;

--
-- Name: activity_calls; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.activity_calls ENABLE ROW LEVEL SECURITY;

--
-- Name: activity_calls activity_calls_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_calls_delete ON public.activity_calls FOR DELETE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_calls.id) AND ((a.created_by = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role))))));


--
-- Name: activity_calls activity_calls_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_calls_insert ON public.activity_calls FOR INSERT TO authenticated WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_calls.id) AND (a.created_by = auth.uid())))));


--
-- Name: activity_calls activity_calls_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_calls_select ON public.activity_calls FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE (a.id = activity_calls.id))));


--
-- Name: activity_calls activity_calls_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_calls_update ON public.activity_calls FOR UPDATE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_calls.id) AND (a.created_by = auth.uid()))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_calls.id) AND (a.created_by = auth.uid())))));


--
-- Name: activity_emails; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.activity_emails ENABLE ROW LEVEL SECURITY;

--
-- Name: activity_emails activity_emails_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_emails_delete ON public.activity_emails FOR DELETE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_emails.id) AND ((a.created_by = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role))))));


--
-- Name: activity_emails activity_emails_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_emails_insert ON public.activity_emails FOR INSERT TO authenticated WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_emails.id) AND (a.created_by = auth.uid())))));


--
-- Name: activity_emails activity_emails_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_emails_select ON public.activity_emails FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE (a.id = activity_emails.id))));


--
-- Name: activity_emails activity_emails_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_emails_update ON public.activity_emails FOR UPDATE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_emails.id) AND (a.created_by = auth.uid()))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_emails.id) AND (a.created_by = auth.uid())))));


--
-- Name: activity_notes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.activity_notes ENABLE ROW LEVEL SECURITY;

--
-- Name: activity_notes activity_notes_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_notes_delete ON public.activity_notes FOR DELETE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_notes.id) AND ((a.created_by = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role))))));


--
-- Name: activity_notes activity_notes_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_notes_insert ON public.activity_notes FOR INSERT TO authenticated WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_notes.id) AND (a.created_by = auth.uid())))));


--
-- Name: activity_notes activity_notes_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_notes_select ON public.activity_notes FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE (a.id = activity_notes.id))));


--
-- Name: activity_notes activity_notes_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY activity_notes_update ON public.activity_notes FOR UPDATE TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_notes.id) AND (a.created_by = auth.uid()))))) WITH CHECK ((EXISTS ( SELECT 1
   FROM public.activities a
  WHERE ((a.id = activity_notes.id) AND (a.created_by = auth.uid())))));


--
-- Name: api_keys; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;

--
-- Name: api_keys api_keys_admin; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY api_keys_admin ON public.api_keys TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role)) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: approvals; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.approvals ENABLE ROW LEVEL SECURITY;

--
-- Name: approvals approvals_decide; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY approvals_decide ON public.approvals FOR UPDATE TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: approvals approvals_raise; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY approvals_raise ON public.approvals FOR INSERT TO authenticated WITH CHECK (true);


--
-- Name: approvals approvals_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY approvals_read ON public.approvals FOR SELECT TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = approvals.lead_id) AND (l.assigned_rep_id = public.my_rep_id()))))));


--
-- Name: assignments assign_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY assign_delete ON public.assignments FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: assignments assign_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY assign_select ON public.assignments FOR SELECT TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (rep_id = public.my_rep_id())));


--
-- Name: assignments assign_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY assign_update ON public.assignments FOR UPDATE TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: assignments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.assignments ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.audit_log ENABLE ROW LEVEL SECURITY;

--
-- Name: audit_log audit_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY audit_select ON public.audit_log FOR SELECT TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: clients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.clients ENABLE ROW LEVEL SECURITY;

--
-- Name: clients clients_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY clients_access ON public.clients TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (account_owner = public.my_rep_id()))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (account_owner = public.my_rep_id())));


--
-- Name: contracts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.contracts ENABLE ROW LEVEL SECURITY;

--
-- Name: contracts contracts_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY contracts_access ON public.contracts TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = contracts.client_id) AND (c.account_owner = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = contracts.client_id) AND (c.account_owner = public.my_rep_id()))))));


--
-- Name: deliverables; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.deliverables ENABLE ROW LEVEL SECURITY;

--
-- Name: deliverables deliverables_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY deliverables_access ON public.deliverables TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = deliverables.client_id) AND (c.account_owner = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = deliverables.client_id) AND (c.account_owner = public.my_rep_id()))))));


--
-- Name: webhook_deliveries deliveries_admin_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY deliveries_admin_read ON public.webhook_deliveries FOR SELECT TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: escalations esc_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY esc_delete ON public.escalations FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: escalations esc_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY esc_select ON public.escalations FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = escalations.lead_id))));


--
-- Name: escalations esc_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY esc_update ON public.escalations FOR UPDATE TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: escalations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.escalations ENABLE ROW LEVEL SECURITY;

--
-- Name: event_outbox; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.event_outbox ENABLE ROW LEVEL SECURITY;

--
-- Name: team_invitations invitations_admin_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY invitations_admin_all ON public.team_invitations TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: lead_stage_history; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.lead_stage_history ENABLE ROW LEVEL SECURITY;

--
-- Name: leads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

--
-- Name: leads leads_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY leads_delete ON public.leads FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: leads leads_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY leads_insert ON public.leads FOR INSERT TO authenticated WITH CHECK (((assigned_rep_id IS NULL) AND (assigned_at IS NULL) AND (stage = ANY (ARRAY['new'::text, 'scoring'::text, 'queued'::text]))));


--
-- Name: leads leads_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY leads_select ON public.leads FOR SELECT TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (assigned_rep_id IS NULL) OR (assigned_rep_id = public.my_rep_id())));


--
-- Name: leads leads_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY leads_update ON public.leads FOR UPDATE TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (assigned_rep_id IS NULL) OR (assigned_rep_id = public.my_rep_id()))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (assigned_rep_id = public.my_rep_id())));


--
-- Name: lead_stage_history lsh_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY lsh_select ON public.lead_stage_history FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = lead_stage_history.lead_id))));


--
-- Name: meetings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.meetings ENABLE ROW LEVEL SECURITY;

--
-- Name: meetings meetings_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY meetings_access ON public.meetings TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = meetings.lead_id) AND (l.assigned_rep_id = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = meetings.lead_id) AND (l.assigned_rep_id = public.my_rep_id()))))));


--
-- Name: model_registry; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.model_registry ENABLE ROW LEVEL SECURITY;

--
-- Name: model_registry mr_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY mr_delete ON public.model_registry FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: model_registry mr_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY mr_insert ON public.model_registry FOR INSERT TO authenticated WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: model_registry mr_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY mr_select ON public.model_registry FOR SELECT TO authenticated USING (true);


--
-- Name: model_registry mr_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY mr_update ON public.model_registry FOR UPDATE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role)) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: nurture_enrolments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.nurture_enrolments ENABLE ROW LEVEL SECURITY;

--
-- Name: nurture_enrolments nurture_enrolments_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nurture_enrolments_access ON public.nurture_enrolments TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = nurture_enrolments.lead_id) AND (l.assigned_rep_id = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = nurture_enrolments.lead_id) AND (l.assigned_rep_id = public.my_rep_id()))))));


--
-- Name: nurture_sequences; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.nurture_sequences ENABLE ROW LEVEL SECURITY;

--
-- Name: nurture_sequences nurture_sequences_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nurture_sequences_read ON public.nurture_sequences FOR SELECT TO authenticated USING (true);


--
-- Name: nurture_sequences nurture_sequences_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nurture_sequences_write ON public.nurture_sequences TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: nurture_steps; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.nurture_steps ENABLE ROW LEVEL SECURITY;

--
-- Name: nurture_steps nurture_steps_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nurture_steps_read ON public.nurture_steps FOR SELECT TO authenticated USING (true);


--
-- Name: nurture_steps nurture_steps_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY nurture_steps_write ON public.nurture_steps TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: offers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.offers ENABLE ROW LEVEL SECURITY;

--
-- Name: offers offers_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY offers_access ON public.offers TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = offers.lead_id) AND (l.assigned_rep_id = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = offers.lead_id) AND (l.assigned_rep_id = public.my_rep_id()))))));


--
-- Name: org_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.org_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: org_settings org_settings_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY org_settings_read ON public.org_settings FOR SELECT TO authenticated USING (true);


--
-- Name: org_settings org_settings_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY org_settings_write ON public.org_settings FOR UPDATE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role)) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: event_outbox outbox_admin_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY outbox_admin_read ON public.event_outbox FOR SELECT TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: payments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;

--
-- Name: payments payments_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY payments_access ON public.payments TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = payments.lead_id) AND (l.assigned_rep_id = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (EXISTS ( SELECT 1
   FROM public.leads l
  WHERE ((l.id = payments.lead_id) AND (l.assigned_rep_id = public.my_rep_id()))))));


--
-- Name: predictions pred_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pred_delete ON public.predictions FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: predictions pred_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pred_insert ON public.predictions FOR INSERT TO authenticated WITH CHECK ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = predictions.lead_id))));


--
-- Name: predictions pred_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY pred_select ON public.predictions FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = predictions.lead_id))));


--
-- Name: predictions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.predictions ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles profiles_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profiles_delete ON public.profiles FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: profiles profiles_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profiles_insert ON public.profiles FOR INSERT TO authenticated WITH CHECK ((id = auth.uid()));


--
-- Name: profiles profiles_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profiles_select ON public.profiles FOR SELECT TO authenticated USING (((id = auth.uid()) OR (public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))));


--
-- Name: profiles profiles_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY profiles_update ON public.profiles FOR UPDATE TO authenticated USING (((id = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role))) WITH CHECK (((id = auth.uid()) OR (public.current_role_name() = 'admin'::public.user_role)));


--
-- Name: project_members; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.project_members ENABLE ROW LEVEL SECURITY;

--
-- Name: project_members project_members_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY project_members_read ON public.project_members FOR SELECT TO authenticated USING (true);


--
-- Name: project_members project_members_write; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY project_members_write ON public.project_members TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: project_tasks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.project_tasks ENABLE ROW LEVEL SECURITY;

--
-- Name: projects; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;

--
-- Name: projects projects_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY projects_access ON public.projects TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (owner_id = public.my_rep_id()) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = projects.client_id) AND (c.account_owner = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (owner_id = public.my_rep_id()) OR (EXISTS ( SELECT 1
   FROM public.clients c
  WHERE ((c.id = projects.client_id) AND (c.account_owner = public.my_rep_id()))))));


--
-- Name: representatives; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.representatives ENABLE ROW LEVEL SECURITY;

--
-- Name: representatives reps_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY reps_delete ON public.representatives FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: representatives reps_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY reps_insert ON public.representatives FOR INSERT TO authenticated WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: representatives reps_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY reps_select ON public.representatives FOR SELECT TO authenticated USING (true);


--
-- Name: representatives reps_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY reps_update ON public.representatives FOR UPDATE TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role]))) WITH CHECK ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: sla_clocks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sla_clocks ENABLE ROW LEVEL SECURITY;

--
-- Name: sla_policies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sla_policies ENABLE ROW LEVEL SECURITY;

--
-- Name: sla_clocks slac_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slac_delete ON public.sla_clocks FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: sla_clocks slac_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slac_select ON public.sla_clocks FOR SELECT TO authenticated USING ((EXISTS ( SELECT 1
   FROM public.leads l
  WHERE (l.id = sla_clocks.lead_id))));


--
-- Name: sla_policies slap_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slap_delete ON public.sla_policies FOR DELETE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: sla_policies slap_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slap_insert ON public.sla_policies FOR INSERT TO authenticated WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: sla_policies slap_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slap_select ON public.sla_policies FOR SELECT TO authenticated USING (true);


--
-- Name: sla_policies slap_update; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY slap_update ON public.sla_policies FOR UPDATE TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role)) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- Name: project_tasks tasks_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tasks_access ON public.project_tasks TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (assignee_id = public.my_rep_id()) OR (EXISTS ( SELECT 1
   FROM public.projects p
  WHERE ((p.id = project_tasks.project_id) AND (p.owner_id = public.my_rep_id())))))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (assignee_id = public.my_rep_id()) OR (EXISTS ( SELECT 1
   FROM public.projects p
  WHERE ((p.id = project_tasks.project_id) AND (p.owner_id = public.my_rep_id()))))));


--
-- Name: team_invitations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.team_invitations ENABLE ROW LEVEL SECURITY;

--
-- Name: time_entries time_access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY time_access ON public.time_entries TO authenticated USING (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (rep_id = public.my_rep_id()))) WITH CHECK (((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])) OR (rep_id = public.my_rep_id())));


--
-- Name: time_entries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.time_entries ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.usage_events ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_events usage_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY usage_insert ON public.usage_events FOR INSERT TO authenticated WITH CHECK (true);


--
-- Name: usage_events usage_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY usage_read ON public.usage_events FOR SELECT TO authenticated USING ((public.current_role_name() = ANY (ARRAY['admin'::public.user_role, 'manager'::public.user_role])));


--
-- Name: webhook_deliveries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.webhook_deliveries ENABLE ROW LEVEL SECURITY;

--
-- Name: webhooks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.webhooks ENABLE ROW LEVEL SECURITY;

--
-- Name: webhooks webhooks_admin; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY webhooks_admin ON public.webhooks TO authenticated USING ((public.current_role_name() = 'admin'::public.user_role)) WITH CHECK ((public.current_role_name() = 'admin'::public.user_role));


--
-- PostgreSQL database dump complete
--

\unrestrict HYS2EB41JX0ClXAxZ6uOVVras1m1NDBo2WzdaNVAUL5usD2kexRlK8RGhHQyBdb

