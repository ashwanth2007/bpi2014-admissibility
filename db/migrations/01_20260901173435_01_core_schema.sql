-- Supabase migration 20260901173435  01_core_schema
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- FastLead CRM: core schema
-- Every table is fully constrained. Enumerations are CHECK constraints so the
-- database, not the application, is the authority on valid state.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- roles
create type user_role as enum ('admin', 'manager', 'rep');

create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text not null,
  full_name   text not null default '',
  role        user_role not null default 'rep',
  created_at  timestamptz not null default now()
);
comment on table profiles is 'Application user. One row per auth.users row. Role drives every access decision.';

-- ------------------------------------------------------- representatives
create table representatives (
  id                  uuid primary key default gen_random_uuid(),
  profile_id          uuid references profiles(id) on delete set null,
  name                text not null,
  specialisation      text not null default 'general',
  capacity            int  not null default 10 check (capacity between 1 and 500),
  is_available        boolean not null default true,
  experience_years    numeric(4,1) not null default 0 check (experience_years >= 0),
  historical_conv_rate numeric(5,4) not null default 0.10
                        check (historical_conv_rate between 0 and 1),
  created_at          timestamptz not null default now()
);
comment on column representatives.capacity is 'Constraint C2 in the optimiser: maximum concurrent open leads.';

-- ---------------------------------------------------------------- leads
create table leads (
  id                uuid primary key default gen_random_uuid(),
  -- attribution, captured once and never overwritten
  source            text not null check (source in
                      ('web_form','webhook','inbound_call','import','referral','manual')),
  campaign          text,
  channel           text,
  -- contact
  full_name         text not null,
  email             text,
  phone             text,
  company           text,
  job_role          text,
  -- qualification inputs
  priority          text not null default 'medium'
                      check (priority in ('low','medium','high')),
  specialisation    text not null default 'general',
  -- lifecycle state machine
  stage             text not null default 'new' check (stage in
                      ('new','scoring','queued','assigned','contacted','nurturing',
                       'meeting_booked','offer_sent','won','lost')),
  -- model outputs
  conversion_probability numeric(5,4) check (conversion_probability between 0 and 1),
  sla_breach_probability numeric(5,4) check (sla_breach_probability between 0 and 1),
  assigned_rep_id   uuid references representatives(id) on delete set null,
  assigned_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint assigned_needs_rep
    check ((stage in ('new','scoring','queued','lost')) or assigned_rep_id is not null)
);
create index leads_stage_idx on leads(stage);
create index leads_queue_idx on leads(stage, conversion_probability desc);
comment on constraint assigned_needs_rep on leads is
  'A lead past the queue must have an owner. Enforced by the database, not the UI.';

-- ------------------------------------------------- stage history (append only)
create table lead_stage_history (
  id          bigserial primary key,
  lead_id     uuid not null references leads(id) on delete cascade,
  from_stage  text,
  to_stage    text not null,
  changed_by  uuid references profiles(id) on delete set null,
  changed_at  timestamptz not null default now()
);
create index lsh_lead_idx on lead_stage_history(lead_id, changed_at desc);

-- ------------------------------------------------------------ assignments
create table assignments (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid not null references leads(id) on delete cascade,
  rep_id        uuid not null references representatives(id) on delete cascade,
  policy        text not null default 'paa_nsga2',
  confidence    numeric(5,4) check (confidence between 0 and 1),
  assigned_at   timestamptz not null default now()
);
create index assignments_lead_idx on assignments(lead_id);

-- -------------------------------------------------------------- SLA clocks
create table sla_policies (
  id                uuid primary key default gen_random_uuid(),
  priority          text not null unique check (priority in ('low','medium','high')),
  response_hours    int  not null check (response_hours > 0)
);

create table sla_clocks (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid not null unique references leads(id) on delete cascade,
  started_at    timestamptz not null default now(),
  due_at        timestamptz not null,
  stopped_at    timestamptz,
  breached      boolean not null default false,
  constraint due_after_start check (due_at > started_at)
);
create index sla_open_idx on sla_clocks(due_at) where stopped_at is null;

create table escalations (
  id            bigserial primary key,
  lead_id       uuid not null references leads(id) on delete cascade,
  reason        text not null,
  created_at    timestamptz not null default now()
);

-- ------------------------------- activities: EER generalisation (Module 2.3)
-- One supertype, three disjoint subtypes. The subtype tables share the supertype
-- primary key, which is the standard relational mapping of an EER specialisation.
create table activities (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid not null references leads(id) on delete cascade,
  kind          text not null check (kind in ('call','email','note')),
  created_by    uuid references profiles(id) on delete set null,
  occurred_at   timestamptz not null default now(),
  unique (id, kind)
);
create index activities_lead_idx on activities(lead_id, occurred_at desc);

create table activity_calls (
  id             uuid primary key,
  kind           text not null default 'call' check (kind = 'call'),
  duration_secs  int not null check (duration_secs >= 0),
  outcome        text not null check (outcome in ('connected','no_answer','voicemail','busy')),
  foreign key (id, kind) references activities(id, kind) on delete cascade
);

create table activity_emails (
  id          uuid primary key,
  kind        text not null default 'email' check (kind = 'email'),
  subject     text not null,
  opened      boolean not null default false,
  foreign key (id, kind) references activities(id, kind) on delete cascade
);

create table activity_notes (
  id       uuid primary key,
  kind     text not null default 'note' check (kind = 'note'),
  body     text not null,
  foreign key (id, kind) references activities(id, kind) on delete cascade
);

-- ----------------------------------------------------------- predictions
create table model_registry (
  id            uuid primary key default gen_random_uuid(),
  model_name    text not null check (model_name in ('model_a_conversion','model_b_sla')),
  version       text not null,
  auc           numeric(5,4),
  trained_on    text,
  notes         text,
  created_at    timestamptz not null default now(),
  unique (model_name, version)
);

create table predictions (
  id            bigserial primary key,
  lead_id       uuid not null references leads(id) on delete cascade,
  model_name    text not null,
  model_version text not null,
  probability   numeric(5,4) not null check (probability between 0 and 1),
  features      jsonb,
  created_at    timestamptz not null default now()
);
create index predictions_lead_idx on predictions(lead_id, created_at desc);

-- ------------------------------------------------ audit log (append only)
create table audit_log (
  id          bigserial primary key,
  table_name  text not null,
  row_id      text not null,
  action      text not null check (action in ('INSERT','UPDATE','DELETE')),
  actor       uuid,
  changed_at  timestamptz not null default now(),
  old_data    jsonb,
  new_data    jsonb
);
create index audit_table_idx on audit_log(table_name, changed_at desc);
