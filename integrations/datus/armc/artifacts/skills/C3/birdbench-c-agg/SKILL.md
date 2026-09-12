---
name: birdbench-c-agg
description: 'Structured query templates: consumption aggregation and conditional comparison.'
---

# birdbench templates — consumption aggregation and conditional comparison (ARM-C candidate C3 split skill)

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

Known failure modes on this family (never reproduce them): negative-q14, negative-q18, negative-q20, negative-q44

FALLBACK RULE: if the question matches no template above (different entity, measure, grain or filters), do not force a template — derive the SQL natively from the schema in the system prompt. The guards above still apply to every SQL you emit, native or templated.
