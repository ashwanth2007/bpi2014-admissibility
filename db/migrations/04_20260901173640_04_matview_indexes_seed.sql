-- Supabase migration 20260901173640  04_matview_indexes_seed
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- FastLead CRM: reporting materialised view, index study, and seed reference data.

-- Dashboard aggregate. A materialised view because the dashboard is read constantly
-- and the underlying aggregation touches every lead.
create materialized view mv_pipeline_summary as
select
  l.stage,
  count(*)                                              as lead_count,
  round(avg(l.conversion_probability)::numeric, 4)      as avg_conversion,
  round(avg(l.sla_breach_probability)::numeric, 4)      as avg_breach_risk,
  count(*) filter (where sc.breached)                   as breached_count
from leads l
left join sla_clocks sc on sc.lead_id = l.id
group by l.stage;

create unique index mv_pipeline_summary_stage on mv_pipeline_summary(stage);

create or replace function refresh_pipeline_summary() returns void
language sql security definer set search_path = public as $$
  refresh materialized view concurrently mv_pipeline_summary;
$$;

-- The composite index for the queue query. The EXPLAIN study compares the queue
-- lookup with and without this index.
create index leads_priority_queue_idx
  on leads(stage, priority, conversion_probability desc)
  where stage in ('queued','new');

-- Representative workload report: GROUP BY with HAVING plus a correlated subquery.
create or replace view v_rep_workload
with (security_invoker = true) as
select r.id, r.name, r.capacity, r.specialisation,
       count(l.id) filter (where l.stage not in ('won','lost')) as active_load,
       round(100.0 * count(l.id) filter (where l.stage not in ('won','lost'))
             / nullif(r.capacity,0), 1) as utilisation_pct,
       (select count(*) from leads x
         where x.assigned_rep_id = r.id and x.stage = 'won') as won_count
from representatives r
left join leads l on l.assigned_rep_id = r.id
group by r.id, r.name, r.capacity, r.specialisation;

grant select on v_rep_workload to authenticated;

-- ------------------------------------------------------------ seed data
insert into sla_policies(priority, response_hours) values
  ('high', 4), ('medium', 24), ('low', 72)
on conflict (priority) do nothing;

insert into model_registry(model_name, version, auc, trained_on, notes) values
  ('model_a_conversion', 'v3-nodur', 0.8091, 'UCI Bank Marketing 41188 records',
   'Deployable arm, call duration excluded. 3x10-fold CV mean AUC.'),
  ('model_b_sla', 'v3-causal', 0.8071, 'BPI Challenge 2014, 31236 incidents',
   'Leak-free, causal expanding-window entity encodings. Stacked ensemble.')
on conflict (model_name, version) do nothing;

insert into representatives(name, specialisation, capacity, experience_years, historical_conv_rate) values
  ('Priya Raman',    'enterprise', 12, 6.5, 0.2400),
  ('Arjun Mehta',    'smb',        15, 3.0, 0.1800),
  ('Sneha Iyer',     'enterprise', 10, 8.0, 0.2900),
  ('Rahul Verma',    'smb',        18, 1.5, 0.1200),
  ('Divya Nair',     'general',    14, 4.0, 0.2000);
