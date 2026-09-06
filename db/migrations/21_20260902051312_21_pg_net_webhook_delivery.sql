-- Supabase migration 20260902051312  21_pg_net_webhook_delivery
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- REAL-TIME WEBHOOK DELIVERY, FROM THE DATABASE.
--
-- The first version queued events in an outbox and drained it from a Next.js route.
-- Testing caught the flaw immediately: that route runs with no user session, so row
-- level security correctly returned nothing and no event was ever delivered. The fix is
-- not to hand the web tier a service-role key. It is to deliver from the database,
-- which is what a Supabase webhook is.
--
-- pg_net posts asynchronously, so the HTTP call does not block the transaction that
-- caused it. A slow or dead receiver cannot slow down lead creation.
create extension if not exists pg_net with schema extensions;
create extension if not exists pgcrypto with schema extensions;

-- Delivery is now driven by the outbox trigger rather than by a polling loop, so a
-- subscriber sees an event within milliseconds of the change that produced it.
create or replace function public.deliver_event()
returns trigger
language plpgsql
security definer
set search_path = public, extensions
as $$
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

revoke all on function public.deliver_event() from public, anon, authenticated;

drop trigger if exists trg_deliver_event on public.event_outbox;
create trigger trg_deliver_event
  after insert on public.event_outbox
  for each row execute function public.deliver_event();

-- pg_net records each response in its own table. This reconciles them back onto the
-- delivery log, so the webhooks screen shows the real status code rather than "queued".
create or replace function public.reconcile_deliveries()
returns integer
language plpgsql
security definer
set search_path = public, extensions, net
as $$
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

revoke all on function public.reconcile_deliveries() from public, anon;
grant execute on function public.reconcile_deliveries() to authenticated;
