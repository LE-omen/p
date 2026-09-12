---
name: birdbench-c-dim
description: 'Structured query templates: dimension-table filters and distinct lists.'
---

# birdbench templates — dimension-table filters and distinct lists (ARM-C candidate C3 split skill)

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

### T06-distinct-list-join — distinct value list through transaction joins

Use when: question says 'list'/'what kind of'/'which chains/descriptions/currencies' reachable from transactions

Parameters:
- target (enum domain=['ChainID', 'Description', 'Currency', 'Segment']) bind from: the listed noun; target table = gasstations/products/customers
- filters (list) bind from: country/currency/date/time equality predicates from the question

Structure: joins=transactions_1k.CustomerID=customers.CustomerID; transactions_1k.GasStationID=gasstations.GasStationID; transactions_1k.ProductID=products.ProductID (only the tables on the path) | group_by=None | aggregation=none — SELECT DISTINCT target
SQL skeleton: SELECT DISTINCT {target_expr} FROM transactions_1k AS T1 INNER JOIN {mid} ... WHERE {filters}
Output contract: columns=1; rows=as many distinct values as exist (>=1 in this dataset); distinct=DISTINCT is mandatory — without it duplicate join rows appear; ordering=none; rounding=none — no aggregate is emitted; empty=no matching transactions -> empty result (0 rows), do not fabricate

Known failure modes on this family (never reproduce them): negative-q26, negative-q34

FALLBACK RULE: if the question matches no template above (different entity, measure, grain or filters), do not force a template — derive the SQL natively from the schema in the system prompt. The guards above still apply to every SQL you emit, native or templated.
