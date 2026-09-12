---
name: birdbench-c-txn
description: 'Structured query templates: transaction filtering, aggregates and point lookups.'
---

# birdbench templates — transaction filtering, aggregates and point lookups (ARM-C candidate C3 split skill)

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

### T07-txn-filtered-aggregate — filtered aggregate over transactions

Use when: question asks how many transactions / average price over a filtered transaction set; 'morning' means Time < '13:00:00' (recorded reference boundary, verified by negative-q36)

Parameters:
- agg (enum domain=['COUNT(TransactionID)', 'AVG(Price)'])
- filters (list) bind from: country (join), Price condition, Date='YYYY-MM-DD', Time < '13:00:00' for morning

Structure: joins=only tables needed for the filter (gasstations for Country, customers for Currency) | group_by=None | aggregation=single aggregate
SQL skeleton: SELECT {agg} FROM transactions_1k AS T1 INNER JOIN {filter_table} ... WHERE {filters}
Output contract: columns=1; rows=1; rounding=none — AVG(Price) raw, no ROUND; semantic_binding='average total price of transactions' = AVG(Price) per transaction; do NOT multiply Amount*Price unless the question defines total = Amount*Price; null=AVG over zero matching rows is NULL

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

FALLBACK RULE: if the question matches no template above (different entity, measure, grain or filters), do not force a template — derive the SQL natively from the schema in the system prompt. The guards above still apply to every SQL you emit, native or templated.
