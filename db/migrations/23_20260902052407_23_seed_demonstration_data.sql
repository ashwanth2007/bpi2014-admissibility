-- Supabase migration 20260902052407  23_seed_demonstration_data
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- Demonstration data. Every screen was technically correct and looked empty, which is
-- indistinguishable from broken. This fills all eight pipeline stages, North American
-- locations, real attribution, and enough closed history for the reports to compute a
-- conversion rate, an SLA compliance figure and a median response time from actual rows.
--
-- The attribution trigger locks source on update, so everything is set at INSERT.
-- Timestamps are spread over the last 90 days so the charts have shape.

do $$
declare
  v_reps        uuid[];
  v_rep         uuid;
  v_lead        uuid;
  i             integer;
  v_stage       text;
  v_created     timestamptz;
  v_assigned    timestamptz;
  v_conv        numeric;
  v_risk        numeric;
  v_pri         text;
  v_src         text;
  v_city        text;
  v_country     text;
  v_state       text;
  v_first       text;
  v_last        text;
  v_co          text;
  v_role        text;
  v_ind         text;
  v_val         numeric;
  v_breach      boolean;

  firsts  text[] := array['James','Maria','Robert','Jennifer','Michael','Linda','David','Sarah',
                          'Daniel','Emily','Christopher','Ashley','Matthew','Jessica','Andrew',
                          'Amanda','Joshua','Melissa','Ryan','Nicole','Brandon','Stephanie',
                          'Justin','Rachel','Tyler','Laura','Kevin','Megan','Eric','Christina',
                          'Carlos','Sofia','Miguel','Valentina','Diego','Camila','Luis','Isabella',
                          'Ethan','Olivia','Liam','Ava','Noah','Mia','Lucas','Charlotte'];
  lasts   text[] := array['Anderson','Thompson','Martinez','Robinson','Clark','Rodriguez','Lewis',
                          'Walker','Hall','Allen','Young','Hernandez','King','Wright','Lopez',
                          'Hill','Scott','Green','Adams','Baker','Gonzalez','Nelson','Carter',
                          'Mitchell','Perez','Roberts','Turner','Phillips','Campbell','Parker',
                          'Tremblay','Gagnon','Roy','Cote','Bouchard','Morin','Silva','Reyes',
                          'Cruz','Flores','Ramirez','Torres','Ortiz','Chavez','Vargas','Mendoza'];
  cities  text[][] := array[
    ['Austin','Texas','United States'],        ['San Francisco','California','United States'],
    ['New York','New York','United States'],   ['Chicago','Illinois','United States'],
    ['Seattle','Washington','United States'],  ['Denver','Colorado','United States'],
    ['Boston','Massachusetts','United States'],['Atlanta','Georgia','United States'],
    ['Miami','Florida','United States'],       ['Phoenix','Arizona','United States'],
    ['Portland','Oregon','United States'],     ['Nashville','Tennessee','United States'],
    ['Toronto','Ontario','Canada'],            ['Vancouver','British Columbia','Canada'],
    ['Montreal','Quebec','Canada'],            ['Calgary','Alberta','Canada'],
    ['Ottawa','Ontario','Canada'],             ['Edmonton','Alberta','Canada'],
    ['Mexico City','CDMX','Mexico'],           ['Guadalajara','Jalisco','Mexico'],
    ['Monterrey','Nuevo Leon','Mexico'],       ['Puebla','Puebla','Mexico'],
    ['Queretaro','Queretaro','Mexico'],        ['Merida','Yucatan','Mexico']];
  companies text[] := array['Northwind Logistics','Brightpath Health','Cedar Ridge Capital',
    'Vertex Manufacturing','Harborline Freight','Silverleaf Analytics','Ironwood Construction',
    'Cascade Robotics','Bluepeak Insurance','Meridian Legal','Foundry Labs','Copperfield Retail',
    'Lakeside Dental','Summit Fitness','Redwood Energy','Talus Security','Orchard Foods',
    'Quantum Freight','Beacon Property','Trailhead Outdoors','Aurora Biotech','Kestrel Aviation',
    'Pinnacle Staffing','Granite Financial','Willow Creek Spa','Atlas Moving','Juniper Media',
    'Sable Automotive','Everline Telecom','海 Pacific Trade'];
  roles   text[] := array['Chief Executive Officer','Chief Technology Officer','VP of Sales',
    'Director of Operations','Head of Marketing','Founder','Operations Manager',
    'Sales Manager','Procurement Lead','IT Director','General Manager','Owner'];
  inds    text[] := array['Logistics','Healthcare','Financial Services','Manufacturing',
    'Retail','Construction','Technology','Insurance','Legal','Energy','Hospitality','Education'];
  srcs    text[] := array['web_form','referral','inbound_call','webhook','import','manual'];
  utms    text[] := array['google','linkedin','facebook','bing','partner','newsletter','direct'];
  meds    text[] := array['cpc','organic','social','email','referral'];
  camps   text[] := array['q3-inbound','always-on-search','partner-coop','webinar-followup','retargeting'];
  -- 8 stages, weighted so the board is full everywhere rather than top heavy.
  stages  text[] := array['queued','queued','assigned','assigned','contacted','contacted',
                          'nurturing','nurturing','meeting_booked','meeting_booked',
                          'offer_sent','offer_sent','won','won','won','lost','lost'];
begin
  select array_agg(id order by created_at) into v_reps
  from public.representatives
  where name in ('Priya Raman','Arjun Mehta','Sneha Iyer','Rahul Verma','Divya Nair');

  if v_reps is null or array_length(v_reps,1) = 0 then
    select array_agg(id order by created_at) into v_reps from public.representatives limit 5;
  end if;

  for i in 1..140 loop
    v_stage   := stages[1 + (i * 7) % array_length(stages,1)];
    v_created := now() - ((i * 15 + (i % 11) * 7) || ' hours')::interval;
    v_pri     := (array['high','medium','medium','low'])[1 + (i * 3) % 4];
    v_src     := srcs[1 + (i * 5) % array_length(srcs,1)];
    v_first   := firsts[1 + (i * 13) % array_length(firsts,1)];
    v_last    := lasts[1 + (i * 17) % array_length(lasts,1)];
    v_co      := companies[1 + (i * 11) % array_length(companies,1)];
    v_role    := roles[1 + (i * 7) % array_length(roles,1)];
    v_ind     := inds[1 + (i * 5) % array_length(inds,1)];
    v_city    := cities[1 + (i * 19) % array_length(cities,1)][1];
    v_state   := cities[1 + (i * 19) % array_length(cities,1)][2];
    v_country := cities[1 + (i * 19) % array_length(cities,1)][3];
    v_val     := (1500 + ((i * 977) % 48500))::numeric;

    -- Scores span the whole range so the hot and cold colouring is visible rather than
    -- everything sitting in one band.
    v_conv := round((0.06 + ((i * 37) % 88)::numeric / 100.0)::numeric, 4);
    if v_conv > 0.97 then v_conv := 0.97; end if;
    v_risk := round((0.05 + ((i * 53) % 90)::numeric / 100.0)::numeric, 4);
    if v_risk > 0.95 then v_risk := 0.95; end if;

    insert into public.leads (
      full_name, email, phone, company, job_role, industry, website,
      source, campaign, channel, utm_source, utm_medium, utm_campaign,
      referrer_url, landing_page,
      address_line, city, state_region, country, postcode, timezone,
      priority, specialisation, stage,
      conversion_probability, sla_breach_probability,
      deal_value, currency, employee_count, notes, created_at
    ) values (
      v_first || ' ' || v_last,
      lower(v_first || '.' || v_last || '@' || replace(lower(split_part(v_co,' ',1)),'','') || '.com'),
      case v_country
        when 'United States' then '+1 ' || (200 + (i % 700))::text || ' 555 ' || lpad(((i * 37) % 10000)::text, 4, '0')
        when 'Canada'        then '+1 ' || (204 + (i % 600))::text || ' 555 ' || lpad(((i * 41) % 10000)::text, 4, '0')
        else                      '+52 ' || (55 + (i % 40))::text || ' ' || lpad(((i * 43) % 100000000)::text, 8, '0')
      end,
      v_co, v_role, v_ind,
      'https://' || replace(lower(v_co), ' ', '') || '.com',
      v_src,
      camps[1 + (i * 3) % array_length(camps,1)],
      case when v_src in ('web_form','webhook') then 'online' else 'direct' end,
      utms[1 + (i * 7) % array_length(utms,1)],
      meds[1 + (i * 11) % array_length(meds,1)],
      camps[1 + (i * 5) % array_length(camps,1)],
      (array['https://google.com','https://linkedin.com','https://news.ycombinator.com',
             'https://partner.example.com',null])[1 + (i * 13) % 5],
      (array['/pricing','/demo','/case-studies','/contact','/'])[1 + (i * 3) % 5],
      (100 + (i * 7) % 8900)::text || ' ' ||
        (array['Main St','Oak Ave','Market St','Elm Rd','Cedar Ln','Av. Reforma'])[1 + (i * 5) % 6],
      v_city, v_state, v_country,
      lpad(((i * 971) % 99999)::text, 5, '0'),
      case v_country when 'Mexico' then 'America/Mexico_City'
                     when 'Canada' then 'America/Toronto'
                     else 'America/Chicago' end,
      v_pri,
      (array['general','enterprise','smb'])[1 + (i * 3) % 3],
      'queued',                              -- moved to the real stage below
      v_conv, null,
      v_val, 'USD',
      (5 + (i * 13) % 900),
      (array['Asked about SLA guarantees on the pricing page.',
             'Downloaded the integration guide, wants a technical call.',
             'Referred by an existing customer.',
             'Compared us against two competitors on the demo call.',
             'Budget approved, waiting on legal review.',
             null])[1 + (i * 7) % 6],
      v_created
    ) returning id into v_lead;

    -- Anything past the queue has an owner, an SLA clock and a Model B score.
    if v_stage <> 'queued' then
      v_rep      := v_reps[1 + (i % array_length(v_reps,1))];
      v_assigned := v_created + (((i % 9) + 1) || ' hours')::interval;
      v_breach   := (i % 6) = 0;   -- roughly 17 per cent breach, so compliance is not 100

      update public.leads
         set assigned_rep_id = v_rep,
             assigned_at = v_assigned,
             sla_breach_probability = v_risk,
             stage = v_stage,
             last_contacted_at = case when v_stage in ('queued','assigned') then null
                                      else v_assigned + interval '3 hours' end
       where id = v_lead;

      insert into public.sla_clocks (lead_id, started_at, due_at, stopped_at, breached)
      values (
        v_lead, v_assigned,
        v_assigned + (case v_pri when 'high' then 4 when 'medium' then 24 else 72 end || ' hours')::interval,
        case when v_stage in ('won','lost') then v_assigned + interval '2 days' else null end,
        v_breach
      )
      on conflict (lead_id) do nothing;

      insert into public.predictions (lead_id, model_name, model_version, probability, features)
      values
        (v_lead, 'model_a_conversion', 'xgb-tuned-a3-nodur', v_conv,
         jsonb_build_object('contributions', jsonb_build_array(
           jsonb_build_object('feature','poutcome','effect', round((v_conv-0.4)::numeric,2)),
           jsonb_build_object('feature','contact','effect', 0.31),
           jsonb_build_object('feature','job','effect', round((v_conv/3)::numeric,2)),
           jsonb_build_object('feature','month','effect', -0.18)))),
        (v_lead, 'model_b_sla', 'xgb-tuned-leakfree', v_risk,
         jsonb_build_object('contributions', jsonb_build_array(
           jsonb_build_object('feature','Group_Breach_Rate_TE','effect', round((v_risk-0.3)::numeric,2)),
           jsonb_build_object('feature','Queue_Length_At_Open','effect', 0.24),
           jsonb_build_object('feature','Assignment_Delay_Hours','effect', round((v_risk/4)::numeric,2)),
           jsonb_build_object('feature','Is_Business_Hours','effect', -0.21))));

      insert into public.usage_events (kind, model_name, model_version, detail, latency_ms, created_at)
      values ('model_inference','model_a_conversion','xgb-tuned-a3-nodur','lead intake', 30 + (i % 70), v_created),
             ('model_inference','model_b_sla','xgb-tuned-leakfree','after assignment', 28 + (i % 60), v_assigned);
    end if;
  end loop;
end $$;

select stage, count(*) from public.leads group by stage order by count(*) desc;
