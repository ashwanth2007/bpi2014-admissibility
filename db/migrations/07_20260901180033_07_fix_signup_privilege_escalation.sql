-- Supabase migration 20260901180033  07_fix_signup_privilege_escalation
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- CRITICAL SECURITY FIX: client-controlled role assignment at sign up.
--
-- THE HOLE. handle_new_user() read the role straight out of raw_user_meta_data,
-- which is whatever the client sent to /auth/v1/signup. An attacker could POST
-- {"role":"admin"} and be an administrator on their first request. The earlier
-- trg_guard_role_change trigger only guarded UPDATE, so it did not apply here.
--
-- Verified exploitable before this fix: a signup carrying role=admin produced a
-- profile with role=admin.
--
-- THE FIX. The database NEVER trusts user metadata for role. Every new profile is
-- created as 'rep'. Elevation happens only through an explicit, admin-gated function.

create or replace function handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  -- full_name is cosmetic and safe to take from metadata.
  -- role is a PRIVILEGE and is never taken from the client. Always 'rep'.
  insert into public.profiles (id, email, full_name, role)
  values (new.id,
          new.email,
          coalesce(new.raw_user_meta_data->>'full_name', ''),
          'rep')
  on conflict (id) do nothing;
  return new;
end $$;

revoke all on function handle_new_user() from public, anon, authenticated;

-- Belt and braces: block a privileged INSERT into profiles as well, so even a direct
-- insert cannot mint an admin. Only an existing admin, or a trusted internal call
-- with no JWT (service role), may create a non-rep profile.
create or replace function trg_guard_role_insert() returns trigger
language plpgsql security definer set search_path = public as $$
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

revoke all on function trg_guard_role_insert() from public, anon, authenticated;

drop trigger if exists profiles_guard_role_insert on profiles;
create trigger profiles_guard_role_insert
  before insert on profiles
  for each row execute function trg_guard_role_insert();

-- The ONLY sanctioned elevation path. Gated on the caller already being an admin.
create or replace function set_user_role(p_user_id uuid, p_role user_role)
returns jsonb
language plpgsql security definer set search_path = public
as $$
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

revoke all on function set_user_role(uuid, user_role) from public, anon;
grant execute on function set_user_role(uuid, user_role) to authenticated;

-- Restore the intended roles on the three demo accounts, which were created before
-- this fix and must keep working for the demo.
update profiles set role = 'admin'   where email = 'admin@example.invalid';
update profiles set role = 'manager' where email = 'manager@example.invalid';
update profiles set role = 'rep'     where email = 'rep@example.invalid';
