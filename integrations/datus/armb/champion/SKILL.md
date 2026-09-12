---
name: b6-sql-discipline-v2
description: 'Reusable SQL answering discipline for the birdbench gas-card schema:
  one-shot final SQL from the provided schema, recorded-value column semantics
  (no invented products), ratio-of-sums derived aggregates, pre-submit self-check,
  exact join paths and predicates, grouped extremes, complete fixed-domain outputs.'
---

# Birdbench SQL answering discipline

## Execution discipline (applies to every question)

- The full schema, value domains and join keys are already in the context. Never
  run `describe_table`, never query `information_schema`, and never issue
  exploratory SELECTs to "check" columns or values.
- Compose the final SELECT in one shot, execute it exactly once, then submit the
  result. Do not fire several candidate queries in parallel; batches of
  exploratory executions exhaust the turn budget and void the run.
- Trust the stated domains (Segment in KAM/LAM/SME; Currency in EUR/CZK;
  Country in CZE/SVK) instead of sampling the tables.

## Column semantics and derived ratios

- Price and Amount are recorded transaction values. Aggregate each column as-is;
  never multiply columns together (e.g. Price * Amount) or reinterpret a column
  as a unit price unless the question's own formula explicitly says so.
- When a question names a derived ratio (an "aggregate unit price", a share, an
  average of a total), build it from the plain column aggregates over the SAME
  eligible rows: sum (or count) of the named numerator column divided by the sum
  (or count) of the named denominator column.
- Apply every eligibility filter the question states (positive amounts, non-NULL
  values, specific ids) to both numerator and denominator.

## Pre-submit self-check (a read-back, not a tool call)

- Before executing, re-read the question and confirm: each requested output
  column maps to the exact expression the question defines; the column order
  matches; every stated filter is present; the row domain (fixed buckets or
  groups) is complete. Fix the SQL in place if any check fails - a wrong
  one-shot answer is a failed answer, not a fast one.

## Join paths and exact predicates

- `transactions_1k.CustomerID = customers.CustomerID`; `transactions_1k.GasStationID
  = gasstations.GasStationID`; `transactions_1k.ProductID = products.ProductID`;
  `yearmonth.CustomerID = customers.CustomerID`. Chain and country come from
  `gasstations`; card numbers are `transactions_1k.CardID`. INNER JOIN unless the
  question asks for unmatched rows.
- Code literals are exact binary-cased matches (no LOWER/UPPER/LIKE).
- `transactions_1k.Date` is 'YYYY-MM-DD', `Time` is 'HH:MM:SS' (morning is
  `Time < '12:00:00'`); `yearmonth.Date` is 'YYYYMM' (year `SUBSTR(Date,1,4)`,
  month `SUBSTR(Date,5,2)`).

## Aggregation, ratios and differences

- Sub-population counts/sums in one pass: `SUM(IF(cond,1,0))`, `SUM(cond = 'x')`;
  differences are the difference of the two terms.
- Cast before dividing, same filtered set for numerator and denominator:
  `CAST(SUM(...) AS FLOAT) * 100 / COUNT(...)`; zero denominators stay NULL.
- Row counts use `COUNT(col)`; distinct entities use `COUNT(DISTINCT col)`.

## Grouped extremes

- Least/most by an aggregate: `GROUP BY entity ORDER BY SUM(metric) ASC|DESC
  LIMIT 1`; paired (entity, value) answers project both in the same query.
  No invented tiebreakers.

## Fixed-domain completeness

- When a question fixes the output domain (named buckets, listed categories,
  every declared group "even if empty"), emit one row per declared element.
  `COUNT(*)` is naturally 0 for an empty group; wrap sums that must show as
  zero with `COALESCE(SUM(x), 0)`; otherwise SUM over an empty set is NULL.
- Percentage columns over such groups: cast before dividing and round displayed
  non-integers to six decimals only when the question states a rounding rule.

## Answer shape

- Project exactly the requested columns in the requested order, keep the
  requested row order, and return the complete table (all rows, NULLs and
  duplicates preserved).
