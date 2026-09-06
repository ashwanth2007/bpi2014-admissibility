-- Supabase migration 20260902050134  20_commercial_pipeline_stages_5_to_9
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Stages 5 to 9 of the specification: nurture, booking, offer, payment, fulfilment.
-- The lead state machine already had the stage names. Nothing stored what happened in
-- them, so "meeting_booked" was a word rather than a record. These are the tables that
-- make each of those stages a fact with a time, an owner and an outcome.

-- ---------------------------------------------------------------- 5. nurture
create table if not exists public.nurture_sequences (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  description text,
  is_active   boolean not null default true,
  -- The condition that pulls a lead OUT early. Without one a sequence keeps mailing
  -- someone who already replied, which is the fastest way to lose a warm lead.
  exit_on     text not null default 'replied'
              check (exit_on in ('replied','meeting_booked','won','lost','never')),
  created_at  timestamptz not null default now()
);

create table if not exists public.nurture_steps (
  id           uuid primary key default gen_random_uuid(),
  sequence_id  uuid not null references public.nurture_sequences(id) on delete cascade,
  step_order   integer not null,
  channel      text not null check (channel in ('email','call','sms','linkedin','task')),
  delay_hours  integer not null default 24 check (delay_hours >= 0),
  subject      text,
  body         text,
  unique (sequence_id, step_order)
);

create table if not exists public.nurture_enrolments (
  id           uuid primary key default gen_random_uuid(),
  lead_id      uuid not null references public.leads(id) on delete cascade,
  sequence_id  uuid not null references public.nurture_sequences(id) on delete cascade,
  current_step integer not null default 0,
  next_run_at  timestamptz,
  status       text not null default 'active'
               check (status in ('active','completed','exited','paused')),
  exited_reason text,
  enrolled_at  timestamptz not null default now(),
  unique (lead_id, sequence_id)
);

-- ---------------------------------------------------------------- 6. meetings
create table if not exists public.meetings (
  id           uuid primary key default gen_random_uuid(),
  lead_id      uuid not null references public.leads(id) on delete cascade,
  rep_id       uuid references public.representatives(id) on delete set null,
  scheduled_at timestamptz not null,
  duration_mins integer not null default 30,
  location     text,
  meeting_url  text,
  status       text not null default 'scheduled'
               check (status in ('scheduled','held','no_show','cancelled','rescheduled')),
  outcome      text,
  created_at   timestamptz not null default now()
);

-- ---------------------------------------------------------------- 7. offers
create table if not exists public.offers (
  id          uuid primary key default gen_random_uuid(),
  lead_id     uuid not null references public.leads(id) on delete cascade,
  title       text not null,
  amount      numeric(12,2) not null check (amount >= 0),
  currency    text not null default 'USD',
  valid_until date,
  status      text not null default 'draft'
              check (status in ('draft','sent','viewed','accepted','declined','expired')),
  sent_at     timestamptz,
  responded_at timestamptz,
  notes       text,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- 8. payments
-- Modelled as a state transition with a recorded link, not a live gateway. That is the
-- scope line the PRD drew and it has not moved.
create table if not exists public.payments (
  id          uuid primary key default gen_random_uuid(),
  offer_id    uuid references public.offers(id) on delete set null,
  lead_id     uuid not null references public.leads(id) on delete cascade,
  amount      numeric(12,2) not null check (amount >= 0),
  currency    text not null default 'USD',
  method      text check (method in ('card','bank_transfer','upi','paypal','other')),
  payment_link text,
  status      text not null default 'pending'
              check (status in ('pending','paid','failed','refunded')),
  paid_at     timestamptz,
  reference   text,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- 9. fulfilment
create table if not exists public.clients (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid unique references public.leads(id) on delete set null,
  name          text not null,
  email         text,
  phone         text,
  company       text,
  account_owner uuid references public.representatives(id) on delete set null,
  status        text not null default 'active'
                check (status in ('onboarding','active','paused','churned')),
  contract_value numeric(12,2),
  currency      text not null default 'USD',
  started_at    timestamptz not null default now()
);

create table if not exists public.deliverables (
  id          uuid primary key default gen_random_uuid(),
  client_id   uuid not null references public.clients(id) on delete cascade,
  title       text not null,
  description text,
  due_at      timestamptz,
  owner_id    uuid references public.representatives(id) on delete set null,
  status      text not null default 'not_started'
              check (status in ('not_started','in_progress','blocked','delivered','accepted')),
  delivered_at timestamptz,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- policies
-- Same shape as the rest of the schema: a representative reaches rows for leads they
-- own, a manager and an administrator reach everything. The lead is the anchor, so
-- every policy resolves through it rather than duplicating the ownership rule.
do $$
declare
  t text;
begin
  foreach t in array array['nurture_sequences','nurture_steps','nurture_enrolments',
                           'meetings','offers','payments','clients','deliverables']
  loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;

-- Definition tables: everyone signed in may read, only admin and manager may change.
do $$
declare
  t text;
begin
  foreach t in array array['nurture_sequences','nurture_steps'] loop
    execute format($f$
      drop policy if exists %1$s_read on public.%1$s;
      create policy %1$s_read on public.%1$s for select to authenticated using (true);
      drop policy if exists %1$s_write on public.%1$s;
      create policy %1$s_write on public.%1$s for all to authenticated
        using (public.current_role_name() in ('admin','manager'))
        with check (public.current_role_name() in ('admin','manager'));
    $f$, t);
  end loop;
end $$;

-- Lead-anchored tables: visibility follows the lead.
do $$
declare
  t text;
begin
  foreach t in array array['nurture_enrolments','meetings','offers','payments'] loop
    execute format($f$
      drop policy if exists %1$s_access on public.%1$s;
      create policy %1$s_access on public.%1$s for all to authenticated
        using (
          public.current_role_name() in ('admin','manager')
          or exists (select 1 from public.leads l
                     where l.id = %1$s.lead_id
                       and l.assigned_rep_id = public.my_rep_id())
        )
        with check (
          public.current_role_name() in ('admin','manager')
          or exists (select 1 from public.leads l
                     where l.id = %1$s.lead_id
                       and l.assigned_rep_id = public.my_rep_id())
        );
    $f$, t);
  end loop;
end $$;

drop policy if exists clients_access on public.clients;
create policy clients_access on public.clients for all to authenticated
  using (public.current_role_name() in ('admin','manager') or account_owner = public.my_rep_id())
  with check (public.current_role_name() in ('admin','manager') or account_owner = public.my_rep_id());

drop policy if exists deliverables_access on public.deliverables;
create policy deliverables_access on public.deliverables for all to authenticated
  using (
    public.current_role_name() in ('admin','manager')
    or exists (select 1 from public.clients c
               where c.id = deliverables.client_id and c.account_owner = public.my_rep_id())
  )
  with check (
    public.current_role_name() in ('admin','manager')
    or exists (select 1 from public.clients c
               where c.id = deliverables.client_id and c.account_owner = public.my_rep_id())
  );

create index if not exists idx_meetings_lead     on public.meetings (lead_id, scheduled_at desc);
create index if not exists idx_offers_lead       on public.offers (lead_id, created_at desc);
create index if not exists idx_payments_lead     on public.payments (lead_id, created_at desc);
create index if not exists idx_enrolments_due    on public.nurture_enrolments (next_run_at)
  where status = 'active';
create index if not exists idx_deliverables_client on public.deliverables (client_id, status);
