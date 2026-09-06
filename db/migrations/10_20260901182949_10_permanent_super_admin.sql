-- Supabase migration 20260901182949  10_permanent_super_admin
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Permanent super admin: owner@example.invalid
--
-- Created pre-confirmed so sign-in works immediately. The account is protected by a
-- trigger so it cannot be demoted or deleted, including by another administrator.

do $$
declare uid uuid;
begin
  select id into uid from auth.users where email = 'owner@example.invalid';

  if uid is null then
    insert into auth.users (
      instance_id, id, aud, role, email, encrypted_password,
      email_confirmed_at, created_at, updated_at,
      raw_app_meta_data, raw_user_meta_data,
      confirmation_token, recovery_token, email_change_token_new, email_change
    ) values (
      '00000000-0000-0000-0000-000000000000', gen_random_uuid(),
      'authenticated', 'authenticated', 'owner@example.invalid',
      crypt('REDACTED_PASSWORD', gen_salt('bf')),
      now(), now(), now(),
      '{"provider":"email","providers":["email"]}'::jsonb,
      '{"full_name":"Ashwanth S"}'::jsonb,
      '', '', '', ''
    ) returning id into uid;
  else
    -- account already exists: reset the password and confirm it
    update auth.users
       set encrypted_password = crypt('REDACTED_PASSWORD', gen_salt('bf')),
           email_confirmed_at = coalesce(email_confirmed_at, now()),
           updated_at = now()
     where id = uid;
  end if;

  -- The signup trigger forces every new profile to 'rep'. Elevate this one
  -- deliberately, as the database owner, which is the sanctioned path.
  update profiles set role = 'admin', full_name = 'Ashwanth S' where id = uid;

  -- Give the super admin a representative row so my_rep_id() resolves and the
  -- rep-facing screens work when demoing from this account.
  if not exists (select 1 from representatives r where r.profile_id = uid) then
    insert into representatives(profile_id, name, specialisation, capacity,
                                experience_years, historical_conv_rate)
    values (uid, 'Ashwanth S', 'general', 20, 5.0, 0.25);
  end if;
end $$;

-- Protect the account. Even another administrator cannot demote or remove it.
create or replace function trg_protect_super_admin() returns trigger
language plpgsql security definer set search_path = public as $$
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

revoke all on function trg_protect_super_admin() from public, anon, authenticated;

drop trigger if exists profiles_protect_super_admin_upd on profiles;
create trigger profiles_protect_super_admin_upd
  before update on profiles
  for each row execute function trg_protect_super_admin();

drop trigger if exists profiles_protect_super_admin_del on profiles;
create trigger profiles_protect_super_admin_del
  before delete on profiles
  for each row execute function trg_protect_super_admin();

select email, role from profiles order by role;
