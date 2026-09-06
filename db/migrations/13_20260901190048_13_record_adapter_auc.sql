-- Supabase migration 20260901190048  13_record_adapter_auc
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- A 7-model council review on 2026-09-02 returned one unanimous finding: the AUC in
-- this registry described the model on its own benchmark data, not the model as this
-- CRM feeds it. The CRM cannot supply every column, so the adapter clamps the rest at
-- the training median, and that costs real discrimination. The cost was then measured
-- by re-scoring the SAME held-out set with exactly those columns clamped.
--
-- The `auc` column now carries the ADAPTER figure, because that is what a prediction
-- row in this database was actually produced under. The full-feature figure moves into
-- the notes, where it cannot be mistaken for the prototype's accuracy.
update public.model_registry
set auc = 0.6885,
    notes = 'Tuned XGBoost, config A3_tuned_no_duration, train-split model (not a 100 percent refit), so the evaluated model is the served model. Full-feature held-out AUC 0.8185; the 0.6885 recorded here is the same held-out set re-scored with the 11 of 19 columns this CRM cannot supply clamped to the training median. Quote 0.6885 for the prototype and 0.8185 only for the model on its own benchmark data. duration is excluded because call length is only known after the call.'
where model_name = 'model_a_conversion' and version = 'xgb-tuned-a3-nodur';

update public.model_registry
set auc = 0.7097,
    notes = 'Tuned XGBoost on the leak-free feature set, train-split model. Full-feature held-out AUC 0.7927; the 0.7097 recorded here is the same held-out set re-scored under the CRM adapter, which fills 9 of 17 columns. Priority, impact and urgency are deliberately clamped: BPI breach is relative to each priority band own median while a CRM SLA is an absolute clock, so passing priority through gave a CRM high priority lead a LOWER breach probability than reality. Masking it cost 0.7118 to 0.7097, two thousandths, and removed an inverted signal that would have fed the assignment objectives. Group_Breach_Rate_TE is the strongest feature and is a k=20 smoothed target encoding, so it is not personalised for a rep with few closed leads.'
where model_name = 'model_b_sla' and version = 'xgb-tuned-leakfree';
