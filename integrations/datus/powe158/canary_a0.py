"""POWE-158 A0: zero-model SQL canary (engine numeric-scale verification).

Per POWE-157 minimal-verification plan (section 5, A0):
  1. engine probes: version(), @@div_precision_increment, and a read-only CTE
     over integers 1/2/2 comparing AVG(v), ROUND(AVG(v),6),
     CAST(AVG(v) AS DECIMAL(30,10)), ROUND(AVG(CAST(v AS DECIMAL(60,18))),6);
  2. V3-13/V3-14 fixed reference SQL with ONLY the day-mean expression
     replaced (all JOIN/filter logic kept byte-identical), each variant scored
     against the frozen H3 oracle cells (abs tol 5e-7, Decimal-normalized);
  3. non-temporal migration probes: AVG over BIGINT Amount (bare / cast forms)
     vs a Decimal reference computed from the same read; COUNT invariance under
     CAST; NULL boundary: AVG over empty/all-NULL sets stays NULL.

No model requests. Read-only SELECT only. Output: r4/evidence/a0-canary.json
plus per-variant outputs for the distiller lineage.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from decimal import Decimal
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
R4 = HERE.parent
H3EVAL = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861/"
              "powe-155-30296b5c919c/workdir/h3eval")
PKG = H3EVAL / "h3-package"
DB_HOSTNAME = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"

# numeric compare is self-contained (scoring module needs rfc8785, absent in
# the datus runtime venv); tolerance identical to the frozen scorer contract.
TOL = Decimal("0.0000005")

REF_EXPR = {
    "V3-13": "ROUND(AVG(CAST(DATEDIFF(Date,md) AS DECIMAL(60,18))),6)",
    "V3-14": "ROUND(AVG(CAST(ABS(DATEDIFF(Date,md)) AS DECIMAL(60,18))),6)",
}
BARE = {"V3-13": "DATEDIFF(Date,md)", "V3-14": "ABS(DATEDIFF(Date,md))"}
VARIANTS = {
    "ref_inner_cast": lambda b: f"ROUND(AVG(CAST({b} AS DECIMAL(60,18))),6)",
    "bare_avg": lambda b: f"AVG({b})",
    "round6_of_bare": lambda b: f"ROUND(AVG({b}),6)",
    "outer_cast30_10": lambda b: f"CAST(AVG({b}) AS DECIMAL(30,10))",
    "outer_cast20_10": lambda b: f"CAST(AVG({b}) AS DECIMAL(20,10))",
    "inner_cast_noround": lambda b: f"AVG(CAST({b} AS DECIMAL(60,18)))",
    "inner_cast_scale6": lambda b: f"AVG(CAST({b} AS DECIMAL(30,6)))",
}


def conn(ip: str):
    return pymysql.connect(host=ip, port=3306, user="mock_data_readonly",
                           password=os.environ["DB_PASSWORD"], database="birdbench",
                           charset="utf8mb4", cursorclass=pymysql.cursors.Cursor,
                           connect_timeout=20, read_timeout=120)


def cell_mismatches(got_rows, exp_rows):
    bad = []
    if len(got_rows) != len(exp_rows):
        return [{"kind": "row_count", "got": len(got_rows), "exp": len(exp_rows)}]
    for ri, (g, e) in enumerate(zip(got_rows, exp_rows)):
        for ci, (gv, ev) in enumerate(zip(g, e)):
            if isinstance(ev, (int, float, Decimal, str)) and isinstance(gv, (int, float, Decimal, str)):
                try:
                    gd, ed = Decimal(str(gv)), Decimal(str(ev))
                except Exception:
                    if str(gv) != str(ev):
                        bad.append({"kind": "cell", "row": ri, "col": ci, "got": str(gv), "exp": str(ev)})
                    continue
                if abs(gd - ed) > TOL:
                    bad.append({"kind": "cell", "row": ri, "col": ci, "got": str(gv),
                                "exp": str(ev), "absdiff": str(abs(gd - ed))})
    return bad


def main() -> None:
    ip = None
    for _ in range(6):
        try:
            ip = socket.gethostbyname(DB_HOSTNAME)
            break
        except Exception:
            time.sleep(5)
    if not ip:
        raise SystemExit("cannot resolve DB host")
    out = {"resolved_ip": ip, "probes": {}, "v3_variants": {}, "migration": {}}
    c = conn(ip)
    with c.cursor() as cur:
        cur.execute("SELECT version()")
        out["probes"]["version"] = cur.fetchone()[0]
        cur.execute("SELECT @@div_precision_increment")
        out["probes"]["div_precision_increment"] = cur.fetchone()[0]
        cur.execute(
            "WITH v(n) AS (SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 2) "
            "SELECT AVG(n), ROUND(AVG(n),6), CAST(AVG(n) AS DECIMAL(30,10)), "
            "ROUND(AVG(CAST(n AS DECIMAL(60,18))),6), SUM(n)/COUNT(n), "
            "AVG(CAST(n AS DECIMAL(60,18))) FROM v")
        row = cur.fetchone()
        out["probes"]["int_1_2_2"] = {
            "exprs": ["AVG(n)", "ROUND(AVG(n),6)", "CAST(AVG(n) AS DECIMAL(30,10))",
                      "ROUND(AVG(CAST(n AS DECIMAL(60,18))),6)", "SUM(n)/COUNT(n)",
                      "AVG(CAST(n AS DECIMAL(60,18)))"],
            "values": [str(x) for x in row],
            "python_types": [type(x).__name__ for x in row],
            "field_types": [d[1] for d in cur.description]}

        for q in ("V3-13", "V3-14"):
            sql = (PKG / "reference_sql" / f"{q}.sql").read_text(encoding="utf-8").strip()
            assert REF_EXPR[q] in sql, f"{q} reference expr not found"
            oracle = json.loads((PKG / "oracle" / f"{q}.json").read_text(encoding="utf-8"))["rows"]
            res = {}
            for key, mk in VARIANTS.items():
                expr = mk(BARE[q])
                vsql = sql.replace(REF_EXPR[q], expr)
                cur.execute(vsql)
                rows = cur.fetchall()
                bad = cell_mismatches(rows, oracle)
                res[key] = {"mean_expr": expr,
                            "values": [[str(x) for x in r] for r in rows],
                            "field_types": [d[1] for d in cur.description],
                            "n_mismatch_cells": len(bad), "mismatches": bad[:6],
                            "matches_oracle": not bad}
            out["v3_variants"][q] = res

        # ---- non-temporal migration probes (over-CAST counterexamples) ----
        mig = {}
        cur.execute("SELECT AVG(Amount), CAST(AVG(Amount) AS DECIMAL(30,10)), "
                    "ROUND(AVG(CAST(Amount AS DECIMAL(60,18))),6), COUNT(*), "
                    "COUNT(CAST(TransactionID AS DECIMAL(30,10))), "
                    "SUM(Amount), CAST(SUM(Amount) AS DECIMAL(60,18)) FROM transactions_1k")
        row = cur.fetchone()
        cur.execute("SELECT Amount FROM transactions_1k")
        amounts = [Decimal(str(r[0])) for r in cur.fetchall()]
        exact = sum(amounts) / len(amounts)
        mig["avg_amount_bigint"] = {
            "labels": ["AVG(Amount)", "CAST(AVG AS DEC(30,10))", "ROUND(AVG(CAST AS DEC(60,18)),6)",
                       "COUNT(*)", "COUNT(cast PK)", "SUM", "CAST SUM DEC(60,18)"],
            "values": [str(x) for x in row],
            "field_types": [d[1] for d in cur.description],
            "decimal_reference_sum_div_n": str(exact),
            "count_invariant_under_cast": row[3] == row[4]}
        cur.execute("SELECT AVG(Price), AVG(CAST(Price AS DECIMAL(38,6))), "
                    "CAST(AVG(Price) AS DECIMAL(38,6)) FROM transactions_1k")
        row = cur.fetchone()
        mig["avg_price_double"] = {"labels": ["AVG(Price)", "AVG(CAST Price DEC(38,6))",
                                              "CAST(AVG(Price) AS DEC(38,6))"],
                                   "values": [str(x) for x in row],
                                   "field_types": [d[1] for d in cur.description]}
        cur.execute("SELECT AVG(v) FROM (SELECT NULL AS v UNION ALL SELECT NULL) t")
        mig["avg_all_null"] = str(cur.fetchone()[0])
        cur.execute("SELECT AVG(v) FROM (SELECT 1 AS v) t WHERE 1=0")
        mig["avg_empty"] = str(cur.fetchone()[0])
        cur.execute("SELECT AVG(CAST(v AS DECIMAL(60,18))) FROM "
                    "(SELECT NULL AS v UNION ALL SELECT NULL) t")
        mig["avg_cast_all_null"] = str(cur.fetchone()[0])
        out["migration"] = mig
    c.close()
    dest = R4 / "evidence" / "a0-canary.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({
        "probes": out["probes"],
        "v3_pass": {q: {k: v["matches_oracle"] for k, v in r.items()} for q, r in out["v3_variants"].items()},
        "v3_values_bare": {q: r["bare_avg"]["values"] for q, r in out["v3_variants"].items()},
        "migration": mig}, ensure_ascii=False, indent=1))
    print("saved ->", dest)


if __name__ == "__main__":
    main()
