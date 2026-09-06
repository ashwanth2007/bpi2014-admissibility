-- Supabase migration 20260902073736  contract_clauses_use_the_currency_symbol
-- project PROJECT_REF (fastlead-crm), exported 2026-09-06 before deletion

-- The clause text still spelled out "USD 39,326" while the metric strip above it read
-- "$39,326". Two ways of writing the same number on one screen is the thing that makes a
-- page feel unfinished, and the symbol is what the rest of the application uses.
--
-- Only US dollar contracts are touched, because "$" is unambiguous only where the
-- currency is already known, and every contract in this dataset is in USD.

update contracts
   set scope         = replace(scope,         'USD ', '$'),
       payment_terms = replace(payment_terms, 'USD ', '$'),
       sla_terms     = replace(sla_terms,     'USD ', '$'),
       terms         = replace(terms,         'USD ', '$'),
       notes         = replace(notes,         'USD ', '$')
 where currency = 'USD';
