"""POWE-138 ARM-C: build the old-R oracle (CSV zero-based odd qids) by executing
the authorized judgment CSV example_sql under a read-only connection.

Same mechanical construction as POWE-132 E1_build REFERENCE_SQL execution
(independent read-only execution, rows preserved verbatim). Runs in the datus
runtime (pymysql). Secrets from env: DB_PASSWORD.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymysql

HOST = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"
PORT = 3306
USER = "mock_data_readonly"
DB = "birdbench"
CSV = "/home/rongfeng.frf/tmp/debit_card_specializing_accuracy_qa_detailed.csv"


def norm(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    out_path = Path(sys.argv[1])
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=os.environ["DB_PASSWORD"],
                           database=DB, charset="utf8mb4", cursorclass=pymysql.cursors.Cursor)
    oracle = {}
    with open(CSV, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    for i, row in enumerate(rows):
        if i % 2 != 1:  # old R = zero-based odd qids only
            continue
        sql = row["example_sql"].strip().rstrip(";")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                data = [[norm(v) for v in r] for r in cur.fetchall()]
            oracle[str(i)] = {
                "qid": i,
                "question": row["question"],
                "gold": row["answer"],
                "sql": sql,
                "columns": cols,
                "rows": data,
                "status": "SUCCESS",
            }
        except Exception as error:  # noqa: BLE001
            oracle[str(i)] = {"qid": i, "question": row["question"], "sql": sql,
                              "status": f"ERROR:{type(error).__name__}"}
    conn.close()
    doc = {"built_at": datetime.now(timezone.utc).isoformat(), "csv_sha256": hashlib.sha256(Path(CSV).read_bytes()).hexdigest(), "oracle": oracle}
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for v in oracle.values() if v.get("status") == "SUCCESS")
    print(f"oracle entries={len(oracle)} success={ok}")


if __name__ == "__main__":
    main()
