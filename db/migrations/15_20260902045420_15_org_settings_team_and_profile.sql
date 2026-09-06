-- Supabase migration 20260902045420  15_org_settings_team_and_profile
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Settings a business owner changes without a developer, plus the team surface that
-- was missing entirely: there was no way to add a person.

-- ---------------------------------------------------------------- profile fields
alter table public.profiles
  add column if not exists phone      text,
  add column if not exists job_title  text,
  add column if not exists avatar_url text,
  add column if not exists timezone   text default 'Asia/Kolkata';

-- ---------------------------------------------------------------- org settings
-- A single-row table. The check constraint is what makes it a singleton: any second
-- insert violates it, so the application never has to guess which row is current.
create table if not exists public.org_settings (
  id                    boolean primary key default true,
  org_name              text not null default 'FastLead',
  auto_assign           boolean not null default false,
  auto_assign_strategy  text not null default 'round_robin'
                        check (auto_assign_strategy in ('round_robin','least_loaded','model_recommended')),
  sla_high_hours        integer not null default 4  check (sla_high_hours   between 1 and 720),
  sla_medium_hours      integer not null default 24 check (sla_medium_hours between 1 and 720),
  sla_low_hours         integer not null default 72 check (sla_low_hours    between 1 and 720),
  -- Where round robin left off, so the rotation survives a restart instead of
  -- resetting to the first representative every time the process recycles.
  round_robin_cursor    integer not null default 0,
  updated_at            timestamptz not null default now(),
  constraint org_settings_singleton check (id)
);

insert into public.org_settings (id) values (true) on conflict (id) do nothing;

alter table public.org_settings enable row level security;

drop policy if exists org_settings_read on public.org_settings;
create policy org_settings_read on public.org_settings
  for select to authenticated using (true);

drop policy if exists org_settings_write on public.org_settings;
create policy org_settings_write on public.org_settings
  for update to authenticated
  using (public.current_role_name() = 'admin')
  with check (public.current_role_name() = 'admin');

-- ---------------------------------------------------------------- invitations
-- An administrator creates an invitation; the person signs up with that email and the
-- signup trigger reads it. This is how a role above 'rep' is granted without ever
-- letting the person choose their own role, which was the escalation hole closed in
-- migration 07.
create table if not exists public.team_invitations (
  id          uuid primary key default gen_random_uuid(),
  email       text not null unique,
  role        public.user_role not null default 'rep',
  full_name   text,
  invited_by  uuid references auth.users(id) on delete set null,
  accepted_at timestamptz,
  created_at  timestamptz not null default now()
);

alter table public.team_invitations enable row level security;

drop policy if exists invitations_admin_all on public.team_invitations;
create policy invitations_admin_all on public.team_invitations
  for all to authenticated
  using (public.current_role_name() in ('admin','manager'))
  with check (public.current_role_name() = 'admin');

create index if not exists idx_invitations_email on public.team_invitations (lower(email));

-- ---------------------------------------------------------------- signup honours it
-- Replaces the hard-coded 'rep' from migration 07. The role still never comes from
-- anything the signing-up user controls: it comes from an invitation row that only an
-- administrator can create. Absent an invitation, the user is a representative.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
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
