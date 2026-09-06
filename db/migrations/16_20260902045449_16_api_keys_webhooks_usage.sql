-- Supabase migration 20260902045449  16_api_keys_webhooks_usage
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The integration surface: an API key so another system can post leads in, outbound
-- webhooks so this system can push events out, and a usage ledger so the cost of both
-- plus every model inference is visible rather than guessed.

-- ---------------------------------------------------------------- API keys
-- The key itself is NEVER stored. Only a SHA-256 digest is kept, so a database dump
-- does not hand an attacker working credentials. The prefix is stored in clear purely
-- so the UI can show "fl_live_a1b2..." in a list without being able to reconstruct it.
create table if not exists public.api_keys (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  key_prefix   text not null,
  key_hash     text not null unique,
  scopes       text[] not null default '{leads:write}',
  created_by   uuid references auth.users(id) on delete set null,
  last_used_at timestamptz,
  request_count bigint not null default 0,
  revoked_at   timestamptz,
  created_at   timestamptz not null default now()
);

alter table public.api_keys enable row level security;

-- Only an administrator sees or manages keys, and the hash column is never selected by
-- the application: the views and the UI read name, prefix, scopes and timestamps.
drop policy if exists api_keys_admin on public.api_keys;
create policy api_keys_admin on public.api_keys
  for all to authenticated
  using (public.current_role_name() = 'admin')
  with check (public.current_role_name() = 'admin');

create index if not exists idx_api_keys_hash on public.api_keys (key_hash) where revoked_at is null;

-- ---------------------------------------------------------------- webhooks
create table if not exists public.webhooks (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  url           text not null check (url ~* '^https?://'),
  -- Used to sign each delivery so the receiver can verify it came from us.
  secret        text not null,
  events        text[] not null default '{lead.created,lead.assigned,lead.stage_changed,sla.escalated,lead.won,lead.lost}',
  is_active     boolean not null default true,
  created_by    uuid references auth.users(id) on delete set null,
  last_status   integer,
  last_fired_at timestamptz,
  failure_count integer not null default 0,
  created_at    timestamptz not null default now()
);

alter table public.webhooks enable row level security;

drop policy if exists webhooks_admin on public.webhooks;
create policy webhooks_admin on public.webhooks
  for all to authenticated
  using (public.current_role_name() = 'admin')
  with check (public.current_role_name() = 'admin');

-- Every attempt is recorded. A webhook that silently stops firing is worse than one
-- that never existed, because the receiving system keeps trusting stale data.
create table if not exists public.webhook_deliveries (
  id            bigserial primary key,
  webhook_id    uuid references public.webhooks(id) on delete cascade,
  event         text not null,
  payload       jsonb not null,
  status_code   integer,
  error         text,
  duration_ms   integer,
  attempt       integer not null default 1,
  created_at    timestamptz not null default now()
);

alter table public.webhook_deliveries enable row level security;

drop policy if exists deliveries_admin_read on public.webhook_deliveries;
create policy deliveries_admin_read on public.webhook_deliveries
  for select to authenticated
  using (public.current_role_name() in ('admin','manager'));

create index if not exists idx_deliveries_webhook on public.webhook_deliveries (webhook_id, created_at desc);

-- ---------------------------------------------------------------- event outbox
-- Writes to this table come from triggers, so an event is recorded in the SAME
-- transaction as the change that caused it. Dispatch then drains the outbox. This is
-- why a lead can never be created without its webhook event existing: if the insert
-- rolls back, so does the event.
create table if not exists public.event_outbox (
  id           bigserial primary key,
  event        text not null,
  payload      jsonb not null,
  dispatched_at timestamptz,
  created_at   timestamptz not null default now()
);

alter table public.event_outbox enable row level security;

drop policy if exists outbox_admin_read on public.event_outbox;
create policy outbox_admin_read on public.event_outbox
  for select to authenticated
  using (public.current_role_name() in ('admin','manager'));

create index if not exists idx_outbox_undispatched
  on public.event_outbox (created_at) where dispatched_at is null;

-- ---------------------------------------------------------------- usage ledger
create table if not exists public.usage_events (
  id         bigserial primary key,
  kind       text not null check (kind in
               ('model_inference','api_request','webhook_delivery','lead_created',
                'assignment','escalation','page_view')),
  detail     text,
  model_name text,
  model_version text,
  latency_ms integer,
  actor_id   uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

alter table public.usage_events enable row level security;

drop policy if exists usage_insert on public.usage_events;
create policy usage_insert on public.usage_events
  for insert to authenticated with check (true);

drop policy if exists usage_read on public.usage_events;
create policy usage_read on public.usage_events
  for select to authenticated
  using (public.current_role_name() in ('admin','manager'));

create index if not exists idx_usage_kind_time on public.usage_events (kind, created_at desc);
create index if not exists idx_usage_model on public.usage_events (model_name, created_at desc);
