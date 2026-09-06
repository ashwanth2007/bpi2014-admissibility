-- Supabase migration 20260902072219  remove_duplicate_representatives_from_account_creation
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Creating the accounts created a second representative for each person.
--
-- `handle_new_user` fires on an insert into auth.users and creates BOTH a profile and a
-- representative row. The previous migration then linked the ORIGINAL representative to
-- the same profile, so each of the five people ended up as two rows sharing one login:
-- the real one carrying 45 places, 25 leads and 80 hours, and an empty one carrying 10
-- places and nothing at all.
--
-- The empty rows go. The originals keep their history and keep the profile link, which
-- is what the account was created for. Deleting is safe here only because each of these
-- rows is provably empty: no lead, no hour, no task, no project membership.

delete from representatives r
 where r.created_at > '2026-09-02'::date
   and r.capacity = 10
   and r.name in ('Arjun Mehta', 'Divya Nair', 'Priya Raman', 'Rahul Verma', 'Sneha Iyer')
   and not exists (select 1 from leads             where assigned_rep_id = r.id)
   and not exists (select 1 from time_entries      where rep_id         = r.id)
   and not exists (select 1 from project_tasks     where assignee_id    = r.id)
   and not exists (select 1 from project_members   where rep_id         = r.id);

-- Re-point the surviving rows at the accounts, in case the delete above removed the row
-- that happened to hold the link.
update representatives r
   set profile_id = p.id
  from profiles p
 where p.full_name = r.name
   and p.email like '%@example.invalid'
   and r.profile_id is null;
