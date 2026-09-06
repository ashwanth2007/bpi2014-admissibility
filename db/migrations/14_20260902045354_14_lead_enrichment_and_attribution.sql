-- Supabase migration 20260902045354  14_lead_enrichment_and_attribution
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- A lead row held a name, an email and a phone and nothing a salesperson actually needs
-- to work it. This adds the fields a business owner expects to see on a record, plus
-- full marketing attribution, which the project's own research gap 1 depends on: you
-- cannot rank by source quality if you never captured the source properly.

alter table public.leads
  -- where they are, which drives territory and call timing
  add column if not exists address_line   text,
  add column if not exists city           text,
  add column if not exists state_region   text,
  add column if not exists country        text,
  add column if not exists postcode       text,
  add column if not exists timezone       text,
  -- how they found us. Written once at capture and never overwritten, so attribution
  -- survives every later edit. That is enforced by trg_lock_attribution below.
  add column if not exists utm_source     text,
  add column if not exists utm_medium     text,
  add column if not exists utm_campaign   text,
  add column if not exists utm_term       text,
  add column if not exists utm_content    text,
  add column if not exists referrer_url   text,
  add column if not exists landing_page   text,
  -- commercial context
  add column if not exists deal_value     numeric(12,2),
  add column if not exists currency       text default 'USD',
  add column if not exists employee_count integer,
  add column if not exists industry       text,
  add column if not exists website        text,
  add column if not exists linkedin_url   text,
  add column if not exists notes          text,
  add column if not exists tags           text[] default '{}',
  add column if not exists last_contacted_at timestamptz,
  add column if not exists next_follow_up_at timestamptz;

-- Attribution is immutable after capture. A source that can be edited later is not
-- attribution, it is an opinion, and every downstream conversion-by-source number
-- silently becomes wrong. The original values are preserved on any update attempt.
create or replace function public.lock_attribution()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
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

drop trigger if exists trg_lock_attribution on public.leads;
create trigger trg_lock_attribution
  before update on public.leads
  for each row execute function public.lock_attribution();

-- Indexes for the filter, sort and group-by work the leads screen now does over
-- thousands of rows rather than the handful the prototype started with.
create index if not exists idx_leads_stage_created    on public.leads (stage, created_at desc);
create index if not exists idx_leads_rep_stage        on public.leads (assigned_rep_id, stage);
create index if not exists idx_leads_sla_risk         on public.leads (sla_breach_probability desc nulls last);
create index if not exists idx_leads_conv             on public.leads (conversion_probability desc nulls last);
create index if not exists idx_leads_source           on public.leads (source);
create index if not exists idx_leads_priority         on public.leads (priority);
create index if not exists idx_leads_tags             on public.leads using gin (tags);
-- Free-text search across the fields a user actually types into a search box.
create index if not exists idx_leads_search on public.leads using gin (
  to_tsvector('simple',
    coalesce(full_name,'') || ' ' || coalesce(email,'') || ' ' ||
    coalesce(company,'') || ' ' || coalesce(phone,'') || ' ' || coalesce(city,''))
);
