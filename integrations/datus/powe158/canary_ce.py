"""POWE-158 counterexample tests for the distilled EXP-10/EXP-11 (zero-model).

Preregistered counterexample families (r4/freeze/prereg-r4.json):
  CE-1 non-temporal migration: the integer-AVG scale rule on a non-temporal
      aggregate (AVG over BIGINT Amount on a filtered subset whose exact mean
      needs >4 fractional digits) — cast forms must match an exact Decimal
      reference computed from the same rows;
  CE-2 NULL boundary: AVG over an all-NULL/empty set stays NULL with and
      without CAST; unmatched rows preserved in counts;
  CE-3 over-CAST guards: insufficient scale DECIMAL(30,4) FAILS the reference
      (scale >= 6 is necessary); COUNT(*)/integer IDs/text equality unchanged
      under the cast forms (casting them adds nothing and must not change
      results); SUM over integers value-identical under DECIMAL(60,18).

Read-only SELECT only; no model calls. Output: r4/evidence/ce-tests.json
"""
from __future__ import annotations

import json
import os
import socket
import time
from decimal import Decimal
from pathlib import Path

import pymysql

HERE = Path(__file__).resolve().parent
R4 = HERE.parent
DB_HOSTNAME = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"
TOL = Decimal("0.0000005")


def main() -> None:
    ip = None
    for _ in range(6):
        try:
            ip = socket.gethostbyname(DB_HOSTNAME)
            break
        except Exception:
            time.sleep(5)
    assert ip
    c = pymysql.connect(host=ip, port=3306, user="mock_data_readonly",
                        password=os.environ["DB_PASSWORD"], database="birdbench",
                        charset="utf8mb4", connect_timeout=20, read_timeout=120)
    out = {"resolved_ip": ip, "tests": {}}
    with c.cursor() as cur:
        # CE-1 non-temporal migration: AVG(Amount) where exact mean >4 decimals
        cur.execute("SELECT Amount FROM transactions_1k WHERE ProductID = 5")
        amounts = [Decimal(str(r[0])) for r in cur.fetchall()]
        exact = sum(amounts) / len(amounts)
        cur.execute(
            "SELECT AVG(Amount), ROUND(AVG(Amount),6), "
            "CAST(AVG(Amount) AS DECIMAL(30,10)), "
            "AVG(CAST(Amount AS DECIMAL(60,18))) "
            "FROM transactions_1k WHERE ProductID = 5")
        row = [Decimal(str(x)) for x in cur.fetchone()]
        labels = ["AVG(Amount)", "ROUND(AVG(Amount),6)", "CAST(AVG AS DEC(30,10))",
                  "AVG(CAST AS DEC(60,18))"]
        out["tests"]["CE1_nontemporal_integer_avg"] = {
            "n": len(amounts), "exact_decimal_mean": str(exact),
            "values": {l: str(v) for l, v in zip(labels, row)},
            "within_5e7": {l: abs(v - exact) <= TOL for l, v in zip(labels, row)},
            "pass": abs(row[2] - exact) <= TOL and abs(row[3] - exact) <= TOL
                    and abs(row[0] - exact) > TOL}

        # CE-2 NULL boundaries
        cur.execute("SELECT AVG(v), AVG(CAST(v AS DECIMAL(60,18))), COUNT(*), "
                    "SUM(v IS NULL) FROM (SELECT CASE WHEN Amount > 100000000 THEN Amount "
                    "END AS v FROM transactions_1k) t")
        row = cur.fetchone()
        out["tests"]["CE2_null_boundary"] = {
            "labels": ["AVG(case-all-null)", "AVG(CAST case-all-null)", "COUNT(*)", "SUM(v IS NULL)"],
            "values": [str(x) for x in row],
            "avg_all_null_is_null": row[0] is None and row[1] is None,
            "rows_preserved_in_count": row[2] == 1000,
            "pass": row[0] is None and row[1] is None and row[2] == 1000 and row[3] == 1000}

        # CE-3a insufficient scale is wrong (scale >= 6 necessary)
        cur.execute("SELECT CAST(AVG(Amount) AS DECIMAL(30,4)), "
                    "AVG(CAST(Amount AS DECIMAL(30,4))) "
                    "FROM transactions_1k WHERE ProductID = 5")
        row = [Decimal(str(x)) for x in cur.fetchone()]
        out["tests"]["CE3a_insufficient_scale"] = {
            "values": {"CAST(AVG AS DEC(30,4))": str(row[0]),
                       "AVG(CAST AS DEC(30,4))": str(row[1])},
            "exact": str(exact),
            "within_5e7": {"outer4": abs(row[0] - exact) <= TOL, "inner4": abs(row[1] - exact) <= TOL},
            "pass": abs(row[0] - exact) > TOL or abs(row[1] - exact) > TOL}

        # CE-3b counts / IDs / text unchanged under the cast forms
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT CustomerID), "
                    "COUNT(DISTINCT CAST(CustomerID AS DECIMAL(30,10))), "
                    "SUM(Amount) = CAST(SUM(CAST(Amount AS DECIMAL(60,18))) AS DECIMAL(20,0)) "
                    "FROM transactions_1k")
        row = cur.fetchone()
        cur.execute("SELECT Currency FROM customers WHERE Currency = "
                    "CAST('EUR' AS CHAR) LIMIT 1")
        txt = cur.fetchone()
        out["tests"]["CE3b_overcast_invariance"] = {
            "count_star": row[0], "distinct_id": row[1], "distinct_id_cast": row[2],
            "sum_int_equals_cast_sum": bool(row[3]), "text_equality_under_cast": txt is not None,
            "pass": row[1] == row[2] and bool(row[3]) and txt is not None}
    c.close()
    out["all_pass"] = all(t.get("pass") for t in out["tests"].values())
    dest = R4 / "evidence" / "ce-tests.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out["tests"], indent=1))
    print("ALL PASS:", out["all_pass"], "->", dest)


if __name__ == "__main__":
    main()
