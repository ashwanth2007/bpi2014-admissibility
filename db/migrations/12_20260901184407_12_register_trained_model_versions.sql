-- Supabase migration 20260901184407  12_register_trained_model_versions
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The trained models are now served for real by code/serving/app.py, so the registry
-- records the served versions alongside the prototype fallback rather than only the
-- fallback. AUC values are the held-out figures reproduced by export_models.py, not
-- claims: each was recomputed on the same 80/20 split and seed as the published run
-- before the estimator was refit on all rows and persisted.
insert into public.model_registry (model_name, version, auc, trained_on, notes) values
  ('model_a_conversion', 'xgb-tuned-a3-nodur', 0.8185,
   'UCI Bank Marketing, bank-additional-full.csv, 41188 rows',
   'Tuned XGBoost, config A3_tuned_no_duration. duration is excluded because call length is only known after the call, which would make the model undeployable. Served live over HTTP. 8 of 19 features come from CRM request data, the remaining 11 are held at the training median.'),
  ('model_b_sla', 'xgb-tuned-leakfree', 0.7927,
   'BPI Challenge 2014, Rabobank ITSM, 31236 incidents',
   'Tuned XGBoost on the leak-free feature set. The only single model whose gain over the 0.7877 untuned reference has a paired bootstrap 95 percent CI excluding zero. Served live over HTTP. 12 of 17 features come from CRM request data, including the group breach rate, which is the strongest feature.'),
  ('model_a_conversion', 'proto-v1-fallback', null, 'not trained',
   'Calibrated scoring function used only when the model service is unreachable. Written into predictions.model_version so a fallback score is never mistaken for a model score.'),
  ('model_b_sla', 'proto-v1-fallback', null, 'not trained',
   'Calibrated scoring function used only when the model service is unreachable.');
