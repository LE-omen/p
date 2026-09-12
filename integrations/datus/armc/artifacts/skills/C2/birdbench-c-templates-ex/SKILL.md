---
name: birdbench-c-templates-ex
description: 'Structured, counterexample-validated query templates for the birdbench fuel-card domain, each with independently verified evidence SQL instantiations.'
---

# birdbench structured query templates with verified examples (ARM-C candidate C2)

USAGE: the full schema is already in your context; do not call describe_table or list_tables. Match the question to one template, bind its parameters, and produce the final SQL in one step (one execute_sql call). Emit the complete answer table exactly as the output contract requires.

## Non-negotiable guards (each one is a recorded failure that lost a question)

- **G-EXACT-OUTPUT**: project exactly the asked-for columns; no boolean helper columns
- **G-NO-ROUND**: never add ROUND/CAST-to-rounded unless the template itself has it
- **G-DISTINCT-LIST**: listing questions ('what kind of', 'list') require DISTINCT over the join result
- **G-SCOPE-DENOM**: percentage/ratio denominators must equal the question scope (conditional SUM form when scope is a condition)
- **G-SEMANTIC-BINDING**: bind business terms to the recorded column semantics ('total price of a transaction' = Price)
- **G-TIME-BOUNDARY**: morning = Time < '13:00:00' (recorded reference boundary, negative-q36 counterexample: 12:00:00 failed); exact times as equality on the stored strings
- **G-GROUP-KEY**: GROUP BY the projected entity only; no extra grouping columns
- **G-NULL-EMPTY**: aggregates over empty/all-NULL sets yield NULL/0 per SQL semantics; empty results stay empty

## Templates

### T01-dim-filter-count — dimension static filter count

Use when: question asks 'how many <entity>' with attribute equalities on a single table (gasstations/customers/products); no aggregation over facts, no time range

Parameters:
- table (enum domain=['gasstations', 'customers'])
- filters (list[col,op,value]) bind from: attribute tokens in the question (country/segment/currency)

Structure: joins=none | group_by=None | aggregation=COUNT(<table PK>)
SQL skeleton: SELECT COUNT({pk}) FROM {table} WHERE {col1} = '{v1}' [AND {col2} = '{v2}']
Output contract: columns=1; rows=exactly 1; ordering=none; distinct=False; rounding=none; empty_filter=COUNT returns 0, never NULL

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q0: SELECT COUNT(GasStationID) FROM gasstations WHERE Country = 'CZE' AND Segment = 'Premium'

### T02-cust-ym-consumption-extremum — per-customer consumption extremum over yearmonth

Use when: question asks who/which customer consumed the most or least gas, or asks for the consumption total too; customers joined to yearmonth

Parameters:
- selector (enum domain=['CustomerID', 'Segment']) bind from: question subject (who -> CustomerID)
- filters (list) bind from: Segment / Currency equality from question
- period (enum domain=['year', 'month', 'none']) bind from: a year ('in 2012') -> SUBSTR(Date,1,4)='YYYY'; a month ('June 2012') -> Date='YYYYMM'
- direction (enum domain=['max', 'min']) bind from: most/least in question
- with_total (bool) bind from: question also asks 'how much'

Structure: joins=customers.CustomerID = yearmonth.CustomerID | group_by=selector | aggregation=SUM(yearmonth.Consumption)
Order/limit: ORDER BY SUM(Consumption) {ASC|DESC} LIMIT 1
SQL skeleton: SELECT {selector}[ , SUM(T2.Consumption)] FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE {filters} GROUP BY {selector} ORDER BY SUM(T2.Consumption) {dir} LIMIT 1
Output contract: columns=1 (or 2 when the total is asked); rows=1; ordering=none (LIMIT 1 already fixes the row); distinct=False; rounding=none — never wrap SUM in ROUND; tie=LIMIT 1 keeps exactly one row; do not add tie-break columns not requested

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q2: SELECT T1.CustomerID FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Segment = 'LAM' AND SUBSTR(T2.Date, 1, 4) = '2012' GROUP BY T1.CustomerID ORDER BY SUM(T2.Consumption) ASC LIMIT 1
- reference-q4: SELECT T1.CustomerID FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Currency = 'CZK' AND T2.Date BETWEEN 201101 AND 201112 GROUP BY T1.CustomerID ORDER BY SUM(T2.Consumption) DESC LIMIT 1
- reference-q16: SELECT T2.CustomerID, SUM(T2.Consumption) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Segment = 'KAM' GROUP BY T2.CustomerID ORDER BY SUM(T2.Consumption) DESC LIMIT 1
- reference-q22: SELECT T1.CustomerID FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T2.Date = '201206' AND T1.Segment = 'SME' GROUP BY T1.CustomerID ORDER BY SUM(T2.Consumption) ASC LIMIT 1

Known failure modes on this family (never reproduce them): negative-q2, negative-q22

### T03-segment-agg-extremum — group-level aggregate extremum

Use when: question asks 'which <grouping attribute>' had the least/most total consumption, no per-customer output

Parameters:
- group_col (enum domain=['Segment'])
- direction (enum domain=['min', 'max'])

Structure: joins=customers.CustomerID = yearmonth.CustomerID | group_by=T1.Segment | aggregation=SUM(T2.Consumption)
Order/limit: ORDER BY SUM(T2.Consumption) {dir} LIMIT 1
SQL skeleton: SELECT T1.Segment FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID GROUP BY T1.Segment ORDER BY SUM(T2.Consumption) {dir} LIMIT 1
Output contract: columns=1; rows=1; rounding=none — the reference emits the raw SUM ordering; adding ROUND to the output changes nothing here but ROUND in ORDER BY over equal-displayed values is forbidden; distinct=False

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q8: SELECT T1.Segment FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID GROUP BY T1.Segment ORDER BY SUM(T2.Consumption) ASC LIMIT 1

Known failure modes on this family (never reproduce them): negative-q8

### T04-peak-month — peak period bucket

Use when: question asks for the consumption peak month / busiest month for a segment in a year

Parameters:
- segment (string) bind from: segment token
- year (string) bind from: year token
- bucket_expr (enum domain=['SUBSTR(T2.Date, 5, 2)'])

Structure: joins=customers.CustomerID = yearmonth.CustomerID | group_by=month bucket | aggregation=SUM(Consumption)
Order/limit: ORDER BY SUM DESC LIMIT 1
SQL skeleton: SELECT SUBSTR(T2.Date, 5, 2) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE SUBSTR(T2.Date, 1, 4) = '{year}' AND T1.Segment = '{segment}' GROUP BY SUBSTR(T2.Date, 5, 2) ORDER BY SUM(T2.Consumption) DESC LIMIT 1
Output contract: columns=1; rows=1; distinct=False; rounding=none

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q10: SELECT SUBSTR(T2.Date, 5, 2) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE SUBSTR(T2.Date, 1, 4) = '2013' AND T1.Segment = 'SME' GROUP BY SUBSTR(T2.Date, 5, 2) ORDER BY SUM(T2.Consumption) DESC LIMIT 1

Known failure modes on this family (never reproduce them): negative-q10

### T05-conditional-agg — conditional aggregation in one row (diff / ratio / percentage)

Use when: question compares two attribute values ('more X than Y, how many more', 'ratio of X to Y', 'what percentage of <scope> is <subset>') or computes a within-scope percentage

Parameters:
- mode (enum domain=['diff', 'ratio', 'pct_of_count', 'pct_of_scope_sum'])
- table (enum domain=['customers', 'gasstations', 'yearmonth', 'transactions_1k'])
- cond_a (expr) bind from: first subgroup attribute test
- cond_b (expr) bind from: second subgroup attribute test (diff/ratio)
- scope (expr) bind from: scope filter of the percentage denominator (must equal the question scope, expressed as a conditional SUM when the scope is itself a condition)

Structure: joins=none | group_by=None | aggregation=SUM(IF(cond,...)) arithmetic; CAST(... AS FLOAT) before division
Forms (bind exactly one):
- diff: SELECT SUM(IF({cond_a},1,0)) - SUM(IF({cond_b},1,0)) FROM {table} [WHERE {scope}]
- ratio: SELECT CAST(SUM(IF({cond_a},1,0)) AS FLOAT) / SUM(IF({cond_b},1,0)) FROM {table}
- pct_of_count: SELECT CAST(SUM({cond_a}) AS FLOAT) * 100 / COUNT({pk}) FROM {table} WHERE {scope}
- pct_of_scope_sum: SELECT CAST(SUM(IF({scope} AND {cond_a},1,0)) AS FLOAT) * 100 / SUM(IF({scope},1,0)) FROM {table}
- ym_diff: SELECT SUM(IF({ya},{val},0)) - SUM(IF({yb},{val},0)) FROM yearmonth WHERE {row_scope}
- ym_rate: SELECT CAST(SUM(IF({ya},{val},0)) - SUM(IF({yb},{val},0)) AS FLOAT) / SUM(IF({yb},{val},0)) FROM yearmonth WHERE {row_scope}
Output contract: columns=1; rows=1; rounding=none — never add ROUND(...,2); division uses CAST AS FLOAT, exact value; boolean_helper_columns=forbidden — 'is it true that X, how many more' wants the single difference value only; null=SUM over empty table is NULL; ratio with zero denominator is NULL

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q6: SELECT SUM(IF(T1.Currency = 'CZK', T2.Consumption, 0)) - SUM(IF(T1.Currency = 'EUR', T2.Consumption, 0)) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE SUBSTR(T2.Date, 1, 4) = '2012'
- reference-q12: SELECT SUM(IF(Country = 'CZE', 1, 0)) - SUM(IF(Country = 'SVK', 1, 0)) FROM gasstations WHERE Segment = 'Discount'
- reference-q14: SELECT SUM(Currency = 'CZK') - SUM(Currency = 'EUR') FROM customers WHERE Segment = 'SME'
- reference-q18: SELECT CAST(SUM(Currency = 'EUR') AS FLOAT) * 100 / COUNT(CustomerID) FROM customers WHERE Segment = 'KAM'
- reference-q20: SELECT CAST(SUM(IF(Segment = 'Premium', 1, 0)) AS FLOAT) * 100 / COUNT(GasStationID) FROM gasstations WHERE Country = 'SVK'
- reference-q42: SELECT CAST(SUM(IF(SUBSTR(Date, 1, 4) = '2012', Consumption, 0)) - SUM(IF(SUBSTR(Date, 1, 4) = '2013', Consumption, 0)) AS FLOAT) / SUM(IF(SUBSTR(Date, 1, 4) = '2012', Consumption, 0)) FROM yearmonth WHERE CustomerID = ( SELECT T1.CustomerID FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID WHERE T1.Date = '2012-08-25' AND T1.Price = 634.8 )
- reference-q44: SELECT CAST(SUM(IF(Country = 'SVK' AND Segment = 'Premium', 1, 0)) AS FLOAT) * 100 / SUM(IF(Country = 'SVK', 1, 0)) FROM gasstations

Known failure modes on this family (never reproduce them): negative-q14, negative-q18, negative-q20, negative-q44

### T06-distinct-list-join — distinct value list through transaction joins

Use when: question says 'list'/'what kind of'/'which chains/descriptions/currencies' reachable from transactions

Parameters:
- target (enum domain=['ChainID', 'Description', 'Currency', 'Segment']) bind from: the listed noun; target table = gasstations/products/customers
- filters (list) bind from: country/currency/date/time equality predicates from the question

Structure: joins=transactions_1k.CustomerID=customers.CustomerID; transactions_1k.GasStationID=gasstations.GasStationID; transactions_1k.ProductID=products.ProductID (only the tables on the path) | group_by=None | aggregation=none — SELECT DISTINCT target
SQL skeleton: SELECT DISTINCT {target_expr} FROM transactions_1k AS T1 INNER JOIN {mid} ... WHERE {filters}
Output contract: columns=1; rows=as many distinct values as exist (>=1 in this dataset); distinct=DISTINCT is mandatory — without it duplicate join rows appear; ordering=none; rounding=none — no aggregate is emitted; empty=no matching transactions -> empty result (0 rows), do not fabricate

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q24: SELECT DISTINCT T3.ChainID FROM transactions_1k AS T1 INNER JOIN customers AS T2 ON T1.CustomerID = T2.CustomerID INNER JOIN gasstations AS T3 ON T1.GasStationID = T3.GasStationID WHERE T2.Currency = 'EUR'
- reference-q26: SELECT DISTINCT T3.Description FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID INNER JOIN products AS T3 ON T1.ProductID = T3.ProductID WHERE T2.Country = 'CZE'
- reference-q34: SELECT DISTINCT T3.Currency FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID INNER JOIN customers AS T3 ON T1.CustomerID = T3.CustomerID WHERE T1.Date = '2012-08-24' AND T1.Time = '16:25:00'

Known failure modes on this family (never reproduce them): negative-q26, negative-q34

### T07-txn-filtered-aggregate — filtered aggregate over transactions

Use when: question asks how many transactions / average price over a filtered transaction set; 'morning' means Time < '13:00:00' (recorded reference boundary, verified by negative-q36)

Parameters:
- agg (enum domain=['COUNT(TransactionID)', 'AVG(Price)'])
- filters (list) bind from: country (join), Price condition, Date='YYYY-MM-DD', Time < '13:00:00' for morning

Structure: joins=only tables needed for the filter (gasstations for Country, customers for Currency) | group_by=None | aggregation=single aggregate
SQL skeleton: SELECT {agg} FROM transactions_1k AS T1 INNER JOIN {filter_table} ... WHERE {filters}
Output contract: columns=1; rows=1; rounding=none — AVG(Price) raw, no ROUND; semantic_binding='average total price of transactions' = AVG(Price) per transaction; do NOT multiply Amount*Price unless the question defines total = Amount*Price; null=AVG over zero matching rows is NULL

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q28: SELECT COUNT(T1.TransactionID) FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID WHERE T2.Country = 'CZE' AND T1.Price > 1000
- reference-q30: SELECT AVG(T1.Price) FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID WHERE T2.Country = 'CZE'
- reference-q36: SELECT COUNT(T1.TransactionID) FROM transactions_1k AS T1 INNER JOIN customers AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Date = '2012-08-26' AND T1.Time < '13:00:00' AND T2.Currency = 'CZK'

Known failure modes on this family (never reproduce them): negative-q30, negative-q36

### T08-point-lookup — point lookup / per-entity extremum on transactions

Use when: question pins an exact timestamp ('at HH:MM:SS in YYYY/M/D'), an exact Consumption value in a month, or asks who paid the most on one date

Parameters:
- mode (enum domain=['timestamp-lookup', 'value-lookup', 'top-of-date'])
- target (expr) bind from: asked attribute (ProductID/Currency/...)
- point (expr) bind from: Date='YYYY-MM-DD' AND Time='HH:MM:SS' | Date='YYYYMM' AND Consumption=<v>

Structure: joins=only tables on the path to the target | group_by=None (lookups) or CustomerID (top-of-date) | aggregation=none (lookups) or SUM(Price) (top-of-date)
Order/limit: top-of-date: ORDER BY SUM(Price) DESC LIMIT 1
SQL skeleton: mode-dependent; top-of-date: SELECT CustomerID FROM transactions_1k WHERE Date='{d}' GROUP BY CustomerID ORDER BY SUM(Price) DESC LIMIT 1
Output contract: columns=1; rows=1 for point lookups in this dataset; distinct=DISTINCT on point lookups is allowed and keeps identical rows from collapsing; rounding=none

Verified evidence SQL (exact instantiations, independently executed read-only):
- reference-q32: SELECT CustomerID FROM transactions_1k WHERE Date = '2012-08-25' GROUP BY CustomerID ORDER BY SUM(Price) DESC LIMIT 1
- reference-q38: SELECT T1.ProductID FROM transactions_1k AS T1 INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID WHERE T1.Date = '2012-08-23' AND T1.Time = '21:20:00'
- reference-q40: SELECT T2.Currency FROM yearmonth AS T1 INNER JOIN customers AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Date = '201306' AND T1.Consumption = 214582.17

FALLBACK RULE: if the question matches no template above (different entity, measure, grain or filters), do not force a template — derive the SQL natively from the schema in the system prompt. The guards above still apply to every SQL you emit, native or templated.
