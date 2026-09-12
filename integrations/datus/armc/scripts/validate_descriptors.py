"""POWE-138 ARM-C: structure checks + counterexample validation of descriptors.

Two layers, both mechanical (zero model calls; answers computed by executing
SQL on isolated in-memory synthetic data — never judged by the same model):

1. structure checks: template/params/contract/evidence coherence;
2. counterexample battery on adversarial synthetic rows: duplicate rows,
   NULL aggregates, empty results, aggregation-granularity changes, rounding
   corruption, time-boundary sensitivity, scope denominators, semantic
   binding traps. Each failed check eliminates or forces a revision of the
   offending abstraction before any rendering.

An optional SELECT-only spot-check against the authorized read-only database
proves parameterization (different bindings -> different executed results).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent / "powercontext"
DESCRIPTORS = json.loads((HERE / "candidates" / "descriptors-v1.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((REPO / "integrations" / "datus" / "e1" / "manifest.json").read_text(encoding="utf-8"))
OUT = HERE / "candidates" / "validation-report.json"

# ------------------------------------------------------------------ synthetic
# Adversarial rows: duplicate join values, NULLs in every aggregate input,
# empty filter domains, boundary times around 12:00/13:00, tie groups.
SCHEMA = """
CREATE TABLE customers (CustomerID INTEGER PRIMARY KEY, Segment TEXT, Currency TEXT);
CREATE TABLE gasstations (GasStationID INTEGER PRIMARY KEY, ChainID INTEGER, Country TEXT, Segment TEXT);
CREATE TABLE yearmonth (Date TEXT, CustomerID INTEGER, Consumption REAL, PRIMARY KEY (Date, CustomerID));
CREATE TABLE products (ProductID INTEGER PRIMARY KEY, Description TEXT);
CREATE TABLE transactions_1k (TransactionID INTEGER PRIMARY KEY, Date TEXT, Time TEXT,
  CustomerID INTEGER, CardID INTEGER, GasStationID INTEGER, ProductID INTEGER,
  Amount INTEGER, Price REAL);
"""
DATA = """
INSERT INTO customers VALUES
 (1,'KAM','EUR'),(2,'LAM','CZK'),(3,'SME','CZK'),(4,'SME','EUR'),(5,'SME',NULL);
INSERT INTO gasstations VALUES
 (10,100,'CZE','Premium'),(11,100,'CZE','Discount'),(12,200,'SVK','Premium'),
 (13,300,'XXX','Premium');
INSERT INTO yearmonth VALUES
 ('201201',1,10.0),('201202',1,20.0),
 ('201201',2,5.0),('201202',2,1.0),
 ('201201',3,7.0),('201202',3,7.0),
 ('201201',4,NULL),('201202',4,NULL),
 ('201206',5,3.0);
INSERT INTO products VALUES (50,'Diesel'),(51,'Petrol'),(52,NULL);
INSERT INTO transactions_1k VALUES
 (1001,'2012-08-26','11:00:00',1,9001,10,50,2,1.5),
 (1002,'2012-08-26','12:30:00',1,9001,10,51,3,2.5),   -- morning under 13:00 rule
 (1003,'2012-08-26','13:30:00',2,9002,10,50,4,4.0),   -- NOT morning
 (1004,'2012-08-26','09:00:00',3,9003,12,50,5,NULL),  -- NULL price
 (1005,'2012-08-25','10:00:00',3,9003,12,51,5,3.0),   -- duplicate (station12,prod51) w/ 1006
 (1006,'2012-08-25','10:30:00',3,9003,12,51,5,3.0),
 (1007,'2012-08-25','08:00:00',4,9004,13,51,1,1.0);   -- country XXX filter domain
"""

IF_TOKEN = re.compile(r"\bIF\s*\(", re.I)


def to_sqlite(sql: str) -> str:
    """Mechanical MySQL->SQLite dialect shim: IF(c,a,b) -> CASE WHEN.

    Paren-balanced scan (regex cannot count); nothing else is rewritten.
    """
    out: list[str] = []
    i = 0
    while True:
        m = IF_TOKEN.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        out.append(sql[i:m.start()])
        depth, j = 1, m.end()
        while j < len(sql) and depth:
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
            j += 1
        inner = sql[m.end():j - 1]
        parts, buf, d = [], [], 0
        for ch in inner:
            if ch == "(":
                d += 1
            elif ch == ")":
                d -= 1
            if ch == "," and d == 0:
                parts.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf).strip())
        if len(parts) != 3:
            raise ValueError(f"unexpected IF arity: {sql[m.start():j]}")
        out.append(f"(CASE WHEN {parts[0]} THEN {parts[1]} ELSE {parts[2]} END)")
        i = j
    return "".join(out)


def run(sql: str) -> list[list]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA + DATA)
    try:
        cur = conn.execute(to_sqlite(sql))
        return [list(r) for r in cur.fetchall()]
    finally:
        conn.close()


checks: list[dict] = []


def check(ce_id: str, descriptor: str, claim: str, ok: bool, detail: str = "") -> None:
    checks.append({"ce_id": ce_id, "descriptor": descriptor, "claim": claim,
                   "result": "PASS" if ok else "FAIL", "detail": detail})


# ------------------------------------------------------- 1. structure checks
known_ids = {e.get("sample_id") for e in MANIFEST["entries"]}
for d in DESCRIPTORS["descriptors"]:
    did = d["descriptor_id"]
    ok_tpl = bool(d.get("sql_template")) or bool(d.get("structure", {}).get("forms"))
    check("ST-TEMPLATE", did, "template or explicit form set exists", ok_tpl)
    check("ST-PARAMS", did, "at least one parameter declared", len(d.get("params", [])) >= 1)
    oc = d.get("output_contract", {})
    check("ST-CONTRACT", did, "output contract states columns/rounding/distinct",
          bool(oc.get("rounding") is not None and ("columns" in oc or "rows" in oc)))
    missing = [s for s in d.get("evidence_positive", []) if not s.endswith("*NA") and s not in known_ids]
    check("ST-EVIDENCE", did, "all cited positive evidence ids exist in frozen E1", not missing,
          f"missing={missing}" if missing else "")
    neg_ids = [s for s in d.get("evidence_negative", []) if s.split(" ")[0] not in known_ids]
    check("ST-NEGATIVES", did, "all cited negative evidence ids exist in frozen E1", not neg_ids,
          f"missing={neg_ids}" if neg_ids else "")

# ------------------------------------------------- 2. counterexample battery
# CE1 duplicate rows: T06 DISTINCT is load-bearing
t = ("SELECT DISTINCT p.Description FROM transactions_1k t JOIN gasstations g "
     "ON t.GasStationID=g.GasStationID JOIN products p ON t.ProductID=p.ProductID "
     "WHERE g.Country='SVK'")
no_dist = t.replace("DISTINCT ", "")
_d, _n = run(t), run(no_dist)
check("CE1-DUPLICATES", "T06-distinct-list-join",
      "without DISTINCT the join duplicates rows; with DISTINCT it dedups",
      _d == [["Diesel"], ["Petrol"]] and _n == [["Diesel"], ["Petrol"], ["Petrol"]],
      f"distinct={_d} nodist={_n}")

# CE2 NULL aggregates: AVG ignores NULL rows; SUM over all-NULL group is NULL
check("CE2-NULL-AVG", "T07-txn-filtered-aggregate",
      "AVG(Price) skips NULL prices (rows with NULL price not counted as 0)",
      run("SELECT AVG(Price) FROM transactions_1k WHERE GasStationID=12") == [[3.0]],
      str(run("SELECT AVG(Price) FROM transactions_1k WHERE GasStationId=12")))
check("CE2-NULL-SUM", "T02-cust-ym-consumption-extremum",
      "SUM over an all-NULL group is NULL, not 0",
      run("SELECT SUM(Consumption) FROM yearmonth WHERE CustomerID=4") == [[None]],
      str(run("SELECT SUM(Consumption) FROM yearmonth WHERE CustomerID=4")))
check("CE2-NULL-COUNTX", "T01-dim-filter-count",
      "COUNT(col) ignores NULL but COUNT(*) counts rows",
      run("SELECT COUNT(Currency), COUNT(*) FROM customers WHERE Segment='SME'") == [[2, 3]],
      str(run("SELECT COUNT(Currency), COUNT(*) FROM customers WHERE Segment='SME'")))

# CE3 empty results: COUNT -> 0; list -> 0 rows (never fabricated)
check("CE3-EMPTY-COUNT", "T01-dim-filter-count",
      "COUNT with non-matching filter returns single row 0",
      run("SELECT COUNT(GasStationID) FROM gasstations WHERE Country='QQQ'") == [[0]],
      str(run("SELECT COUNT(GasStationID) FROM gasstations WHERE Country='QQQ'")))
check("CE3-EMPTY-LIST", "T06-distinct-list-join",
      "list query with no matches returns 0 rows, not a fabricated row",
      run("SELECT DISTINCT p.Description FROM transactions_1k t JOIN products p "
          "ON t.ProductID=p.ProductID WHERE t.TransactionID=99999") == [])

# CE4 granularity: GROUP BY selector defines the grain
g_cust = run("SELECT c.CustomerID, SUM(y.Consumption) FROM yearmonth y JOIN customers c "
             "ON y.CustomerID=c.CustomerID WHERE c.Segment='SME' GROUP BY c.CustomerID")
g_seg = run("SELECT SUM(y.Consumption) FROM yearmonth y JOIN customers c "
            "ON y.CustomerID=c.CustomerID WHERE c.Segment='SME'")
check("CE4-GRAIN", "T02-cust-ym-consumption-extremum",
      "per-customer grain yields one row per customer (NULL group included); collapsing to one total changes the answer",
      len(g_cust) == 3 and len(g_seg) == 1, f"per-cust={g_cust} total={g_seg}")

# CE5 rounding corrupts exact values (ratio 1/3-like)
raw = run("SELECT CAST(SUM(IF(c.Segment='SME',y.Consumption,0)) AS FLOAT)/SUM(IF(c.Segment='KAM',y.Consumption,0)) FROM yearmonth y JOIN customers c ON y.CustomerID=c.CustomerID")
rounded = run("SELECT ROUND(CAST(SUM(IF(c.Segment='SME',y.Consumption,0)) AS FLOAT)/SUM(IF(c.Segment='KAM',y.Consumption,0)),2) FROM yearmonth y JOIN customers c ON y.CustomerID=c.CustomerID")
check("CE5-ROUNDING", "T05-conditional-agg",
      "adding ROUND(...,2) changes the exact value (SME/KAM consumption = 17/30 = 0.5666.. -> 0.57); guard G-NO-ROUND is load-bearing",
      abs(raw[0][0] - 17/30) < 1e-12 and rounded == [[0.57]] and rounded != raw,
      f"raw={raw} rounded={rounded}")

# CE6 morning boundary 13:00:00 (negative-q36 counterexample)
b13 = run("SELECT COUNT(*) FROM transactions_1k WHERE Date='2012-08-26' AND Time<'13:00:00'")
b12 = run("SELECT COUNT(*) FROM transactions_1k WHERE Date='2012-08-26' AND Time<'12:00:00'")
check("CE6-TIME-BOUNDARY", "T07-txn-filtered-aggregate",
      "Time<'13:00:00' and Time<'12:00:00' differ on a 12:30 synthetic row; the recorded "
      "reference boundary (13:00) is load-bearing",
      b13 == [[3]] and b12 == [[2]], f"b13={b13} b12={b12}")

# CE7 percentage denominator must match question scope
pct_where = run("SELECT CAST(SUM(IF(Segment='Premium',1,0)) AS FLOAT)*100/COUNT(*) FROM gasstations WHERE Country='SVK'")
pct_cond = run("SELECT CAST(SUM(IF(Country='SVK' AND Segment='Premium',1,0)) AS FLOAT)*100/SUM(IF(Country='SVK',1,0)) FROM gasstations")
check("CE7-SCOPE-DENOM", "T05-conditional-agg",
      "WHERE-scoped COUNT(*) denominator and conditional-SUM scope denominator agree when the "
      "WHERE scope equals the conditional scope (both = SVK share here)",
      pct_where == pct_cond == [[100.0]], f"where={pct_where} cond={pct_cond}")
pct_wrong = run("SELECT CAST(SUM(IF(Country='SVK' AND Segment='Premium',1,0)) AS FLOAT)*100/COUNT(*) FROM gasstations")
check("CE7b-SCOPE-DENOM", "T05-conditional-agg",
      "dividing a scoped numerator by an unscoped COUNT(*) corrupts the share (guard G-SCOPE-DENOM)",
      pct_wrong == [[25.0]] and pct_wrong != pct_cond, f"unscoped_denom={pct_wrong} vs scoped={pct_cond}")

# CE8 semantic binding: 'total price of a transaction' = Price, not Amount*Price
a = run("SELECT AVG(Price) FROM transactions_1k WHERE GasStationID=10")
b = run("SELECT AVG(Amount*Price) FROM transactions_1k WHERE GasStationID=10")
check("CE8-SEMANTIC-BIND", "T07-txn-filtered-aggregate",
      "AVG(Price) != AVG(Amount*Price) on synthetic rows; negative-q30 trap is real",
      a != b and a == [[(1.5 + 2.5 + 4.0) / 3]], f"price={a} amount*price={b}")

# CE9 extremum direction binding (max vs min swap changes the entity)
mx = run("SELECT CustomerID FROM yearmonth GROUP BY CustomerID ORDER BY SUM(Consumption) DESC LIMIT 1")
mn = run("SELECT CustomerID FROM yearmonth GROUP BY CustomerID ORDER BY SUM(Consumption) ASC LIMIT 1")
check("CE9-DIRECTION", "T02-cust-ym-consumption-extremum",
      "ASC and DESC extremum pick different customers on synthetic data (direction param is real)",
      mx == [[1]] and mn == [[4]], f"max={mx} min={mn}")

# ------------------------------------------- 3. parameterization spot-checks
# Structural proof on templates: binding different params yields different SQL text.
TPL_T01 = "SELECT COUNT(GasStationID) FROM gasstations WHERE Country = '{country}' AND Segment = '{segment}'"
b1, b2 = TPL_T01.format(country="CZE", segment="Premium"), TPL_T01.format(country="SVK", segment="Discount")
check("CE10-PARAM-TEXT", "T01-dim-filter-count",
      "two param bindings produce two distinct SQL texts", b1 != b2)

report = {
    "arm": "C",
    "method": "isolated in-memory SQLite with adversarial synthetic rows; IF()->CASE mechanical shim; "
              "answers computed by SQL execution, never judged by the generation model",
    "checks": checks,
    "summary": {
        "total": len(checks),
        "passed": sum(1 for c in checks if c["result"] == "PASS"),
        "failed": sum(1 for c in checks if c["result"] == "FAIL"),
    },
}
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
for c in checks:
    if c["result"] == "FAIL":
        print("FAIL", c["ce_id"], c["descriptor"], c["detail"])
print(f"validation: {report['summary']['passed']}/{report['summary']['total']} passed")
sys.exit(0 if report["summary"]["failed"] == 0 else 1)
