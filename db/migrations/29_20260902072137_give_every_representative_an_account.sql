-- Supabase migration 20260902072137  give_every_representative_an_account
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Every representative gets a login.
--
-- Five people carried leads, logged hours and appeared on the team page reading "no
-- account, cannot sign in", which is a sentence about our seed data rather than about
-- them. The view already supported an accountless representative, and it still does,
-- but nobody in this dataset should be one.
--
-- Emails are firstname.lastname@example.invalid, the password is the shared demo password,
-- and the rows are pre-confirmed exactly the way migration 10 created the demo users,
-- because the confirmation mailer is rate limited and this is a prototype dataset.

do $$
declare
  r record;
  new_id uuid;
  addr text;
begin
  for r in
    select id, name, specialisation
    from representatives
    where profile_id is null
    order by name
  loop
    new_id := gen_random_uuid();
    addr := lower(regexp_replace(r.name, '\s+', '.', 'g')) || '@example.invalid';

    insert into auth.users (
      id, instance_id, aud, role, email, encrypted_password,
      email_confirmed_at, created_at, updated_at,
      raw_app_meta_data, raw_user_meta_data,
      confirmation_token, recovery_token, email_change_token_new, email_change
    ) values (
      new_id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
      addr, crypt('REDACTED_PASSWORD', gen_salt('bf')),
      now(), now(), now(),
      '{"provider":"email","providers":["email"]}'::jsonb,
      jsonb_build_object('full_name', r.name),
      '', '', '', ''
    );

    -- The handle_new_user trigger may already have written the profile row. Fill in the
    -- rest either way rather than assuming which path ran.
    insert into profiles (id, email, full_name, role)
    values (new_id, addr, r.name, 'rep')
    on conflict (id) do update
      set email = excluded.email, full_name = excluded.full_name;

    update representatives set profile_id = new_id where id = r.id;
  end loop;
end $$;

-- The signup suite leaves two throwaway users behind on every run. They own no lead, no
-- hour and no task (verified before writing this), and they are not people, so they do
-- not belong on a team page.
delete from representatives
 where name in ('Attacker', 'New User')
   and not exists (select 1 from leads where assigned_rep_id = representatives.id)
   and not exists (select 1 from time_entries where rep_id = representatives.id);
