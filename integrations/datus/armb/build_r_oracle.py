"""POWE-137 ARM-B: build the old-R (zero-based odd qid) expected-rows oracle.

Evaluator-side material: executes the authorized judgment CSV's example_sql
for the odd rows (1,3,...,45) under a read-only, SELECT-only controlled
connection - the same treatment E1 gave the learning half (POWE-132
e1_build.execute_reference). No candidate context executes or reads this
before the champion freeze; it is scoring input only.

Usage: python build_r_oracle.py --csv JUDGMENT_CSV --out r-oracle.json
Secrets: DB_PASSWORD from environment (never printed).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROW_CAP = 10_000
MULTI_STATEMENT = re.compile(r";\s*\S")
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|lock|load\s+data|call|set\b)",
    re.IGNORECASE,
)
EXPECTED_CSV_SHA = "c4507c1cd1d167a4c2b05d4cfad7f48226b45e216f2826127070b562e89bda84"

DB = {
    "host": "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com",
    "port": 3306,
    "user": "mock_data_readonly",
    "database": "birdbench",
}

# Old-R questions are unordered unless the text explicitly demands an order on
# a multi-row output (frozen comparator rule: ordered only on explicit demand).
EXPLICIT_ORDER_PATTERNS = (
    "in order",
    "ordered",
    "sorted",
    "ranking",
    "rank them",
    "from highest to lowest",
    "from lowest to highest",
)


def assert_readonly_select(sql: str) -> None:
    stripped = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError(f"reference SQL is not SELECT/WITH-only: {sql[:60]!r}")
    if MULTI_STATEMENT.search(stripped) or FORBIDDEN.search(stripped):
        raise ValueError(f"reference SQL contains forbidden syntax: {sql[:60]!r}")


def normalize_cell(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--out", required=True)
    options = parser.parse_args()

    raw = Path(options.csv).read_bytes()
    csv_sha = hashlib.sha256(raw).hexdigest()
    if csv_sha != EXPECTED_CSV_SHA:
        raise SystemExit(f"CSV fingerprint drift: {csv_sha} != {EXPECTED_CSV_SHA}")

    with open(options.csv, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    odd = [(i, r) for i, r in enumerate(rows) if i % 2 == 1]
    if len(odd) != 23:
        raise SystemExit(f"expected 23 odd rows, got {len(odd)}")

    import pymysql

    connection = pymysql.connect(
        host=DB["host"], port=DB["port"], user=DB["user"],
        password=os.environ["DB_PASSWORD"], database=DB["database"],
        charset="utf8mb4", connect_timeout=15,
    )
    oracle: dict[str, dict] = {}
    try:
        with connection.cursor() as cursor:
            for qid, row in odd:
                sql = row["example_sql"].strip()
                assert_readonly_select(sql)
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                data = [[normalize_cell(v) for v in r] for r in cursor.fetchmany(ROW_CAP + 1)]
                if len(data) > ROW_CAP:
                    raise ValueError(f"q{qid} exceeded row cap")
                question = row["question"].strip()
                oracle[str(qid)] = {
                    "qid": qid,
                    "question": question,
                    "sql": sql,
                    "csv_answer": row["answer"],
                    "columns": columns,
                    "rows": data,
                    "row_count": len(data),
                    "executed_at": datetime.now(timezone.utc).isoformat(),
                    # single-row outputs are order-neutral; multi-row outputs only
                    # order-checked when the question text demands an order
                    "ordered": len(data) > 1 and any(p in question.lower() for p in EXPLICIT_ORDER_PATTERNS),
                }
    finally:
        connection.close()

    payload = {
        "arm": "B",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "csv_sha256": csv_sha,
        "partition": "old-R (zero-based odd qids 1,3,...,45)",
        "expected": oracle,
    }
    Path(options.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    multi = sum(1 for v in oracle.values() if v["row_count"] > 1)
    ordered = [k for k, v in oracle.items() if v["ordered"]]
    print(f"r-oracle built: {len(oracle)} questions, multi-row={multi}, ordered={ordered}")


if __name__ == "__main__":
    sys.exit(main())
