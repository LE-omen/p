# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build the DESIGN-6 E2 evidence pool manifest (POWE-145, B+C merged arm).

E2 = the frozen E1 pool (REFERENCE_SQL/native/negative/unverified origins,
unchanged) plus:

- REFERENCE_SQL_D2: the released D2 v2.1 package's reference SQL for all 23
  learning-side questions, each executed independently under a read-only,
  SELECT-only controlled connection and cross-verified against the package
  oracle under the preregistered Decimal-tolerance comparator.
- negative (partition D2): failure-feedback negatives harvested from B-mechanism
  runs on the D2 side (agent's bound final SQL vs the D2 reference rows); each
  supplied entry names its source run for lineage.

The odd validation half (old R) still never enters E2. The manifest keeps the
per-origin ledger; REFERENCE_SQL + REFERENCE_SQL_D2 + native records describe
at most 23 + 23 questions and are not independent samples.

Runs in the main repository environment; the only secret is DB_PASSWORD from
the environment.

  PYTHONPATH=integrations/datus/src uv run --frozen python integrations/datus/e2_build.py \
      --d2-dir D2_PACKAGE_DIR --out-dir integrations/datus/e2
"""

# ruff: noqa: TRY003 - bounded validation errors are part of this script's diagnostics.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "datus" / "src"))

from powercontext_datus.freeze import digest_json  # noqa: E402
from powercontext_datus.learning import LearningEvidence  # noqa: E402
from powercontext_datus.trace_learning import rows_equal  # noqa: E402

ROW_CAP = 10_000
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|create|alter|drop|truncate|rename|grant|revoke|lock|call|load|handler|merge|set)\b",
    re.IGNORECASE,
)
_MULTI_STATEMENT = re.compile(r";.+")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_readonly_select(sql: str) -> None:
    stripped = sql.strip().rstrip(";").strip()
    if not re.match(r"^(\(\s*)*(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError(f"reference SQL is not SELECT/WITH-only: {sql[:60]!r}")
    if _MULTI_STATEMENT.search(stripped) or _FORBIDDEN.search(stripped):
        raise ValueError(f"reference SQL contains forbidden syntax: {sql[:60]!r}")


def normalize_cell(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        # Exact decimal text; the preregistered comparator treats numeric
        # strings as numbers after Decimal normalization.
        return format(value, "f")
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def execute_d2_reference(items: list[dict], db: dict) -> dict[str, dict]:
    import pymysql

    connection = pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=os.environ["DB_PASSWORD"],
        database=db["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        connect_timeout=15,
    )
    results: dict[str, dict] = {}
    try:
        with connection.cursor() as cursor:
            for item in items:
                assert_readonly_select(item["sql"])
                cursor.execute(item["sql"])
                columns = [d[0] for d in cursor.description] if cursor.description else []
                data = [[normalize_cell(v) for v in row] for row in cursor.fetchmany(ROW_CAP + 1)]
                if len(data) > ROW_CAP:
                    raise ValueError(f"reference {item['qid']} exceeded row cap {ROW_CAP}")
                results[item["qid"]] = {"columns": columns, "rows": data}
    finally:
        connection.close()
    return results


def reference_d2_entry(qid: str, question: str, sql: str, executed: dict) -> dict:
    evidence = LearningEvidence(
        sample_id=f"reference-d2-{qid}",
        question=question,
        sql=sql,
        result_digest=digest_json(executed["rows"]),
        native_receipt_digest=digest_json({
            "origin": "REFERENCE_SQL_D2",
            "qid": qid,
            "columns": executed["columns"],
            "row_count": len(executed["rows"]),
        }),
        source_manifest_digest=digest_json({
            "source": "D2 v2.1 released package",
            "qid": qid,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        }),
        lesson=(
            "Reference SQL from the released D2 v2.1 learning package, independently executed against "
            "the read-only database under a SELECT-only controlled connection and cross-verified "
            "against the package oracle under the preregistered Decimal-tolerance comparator "
            "(origin=REFERENCE_SQL_D2)."
        ),
    )
    return {
        "origin": "REFERENCE_SQL_D2",
        "qid": qid,
        **vars(evidence),
        "columns": executed["columns"],
        "row_count": len(executed["rows"]),
    }


def load_extra_negatives(path: Path | None) -> list[dict]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload:
        if entry.get("origin") != "negative" or entry.get("source_partition") != "D2":
            raise ValueError("extra negatives must carry origin=negative and source_partition=D2")
        for key in ("qid", "question", "sql", "agent_rows_digest", "reference_rows_digest", "lesson", "source_run"):
            if not entry.get(key):
                raise ValueError(f"extra negative missing field {key}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d2-dir", required=True, help="extracted D2 v2.1 package directory")
    parser.add_argument("--e1-manifest", default=str(Path(__file__).resolve().parent / "e1" / "manifest.json"))
    parser.add_argument("--extra-negatives", default=None, help="JSON list of D2 failure-feedback negatives")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--db-host", default="t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="mock_data_readonly")
    parser.add_argument("--db-name", default="birdbench")
    options = parser.parse_args()

    d2_dir = Path(options.d2_dir)
    questions = json.loads((d2_dir / "questions.json").read_text(encoding="utf-8"))["questions"]
    e1 = json.loads(Path(options.e1_manifest).read_text(encoding="utf-8"))
    extra_negatives = load_extra_negatives(Path(options.extra_negatives) if options.extra_negatives else None)

    db = {"host": options.db_host, "port": options.db_port, "user": options.db_user, "database": options.db_name}
    items = [
        {
            "qid": q["id"],
            "question": q["question"],
            "ordered": bool((q.get("contract") or {}).get("ordered")),
            "sql": (d2_dir / "reference_sql" / f"{q['id']}.sql").read_text(encoding="utf-8").strip().rstrip(";"),
        }
        for q in questions
    ]
    executed = execute_d2_reference(items, db)

    crosscheck = []
    new_entries = []
    oracle_d2: dict[str, dict] = {}
    for item in items:
        oracle = json.loads((d2_dir / "oracle" / f"{item['qid']}.json").read_text(encoding="utf-8"))
        matched = rows_equal(executed[item["qid"]]["rows"], oracle["rows"], ordered=item["ordered"])
        crosscheck.append({"qid": item["qid"], "oracle_matched": matched, "row_count": len(oracle["rows"])})
        if not matched:
            raise SystemExit(f"{item['qid']}: independent execution disagrees with the package oracle; refusing E2")
        new_entries.append(reference_d2_entry(item["qid"], item["question"], item["sql"], executed[item["qid"]]))
        oracle_d2[item["qid"]] = {
            "question": item["question"],
            "ordered": item["ordered"],
            "expected": {"columns": oracle["columns"], "rows": oracle["rows"]},
        }

    entries = list(e1["entries"]) + new_entries + extra_negatives
    origin_counts: dict[str, int] = {}
    for entry in entries:
        origin_counts[entry["origin"]] = origin_counts.get(entry["origin"], 0) + 1

    manifest = {
        "e2_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "base_head": os.environ.get("E2_BASE_HEAD", ""),
        "e1_manifest": {
            "path": str(options.e1_manifest),
            "sha256": sha256_file(Path(options.e1_manifest)),
            "origin_counts": e1.get("origin_counts", {}),
        },
        "d2_package": {
            "dir": str(d2_dir),
            "manifest_sha256": sha256_file(d2_dir / "manifest.json"),
            "question_bank_sha256": sha256_file(d2_dir / "questions.csv"),
        },
        "db": {k: db[k] for k in ("host", "port", "user", "database")},
        "row_cap": ROW_CAP,
        "rules": {
            "origin": {
                "REFERENCE_SQL": "judgment-CSV example_sql for the old learning half (inherited from E1)",
                "REFERENCE_SQL_D2": "released D2 v2.1 package reference SQL, independently executed read-only/SELECT-only and oracle-cross-verified",
                "native": "preserved E0 evidence (inherited from E1)",
                "negative": "complete run with bound final SQL whose rows differ from the reference rows (old or D2 partition)",
                "unverified": "cannot be mechanically certified; never generation input",
            },
            "comparator": (
                "trace_learning.rows_equal under the preregistered POWE-145 numeric rule: Decimal-normalized "
                "numerics (dict members included) with absolute tolerance 5e-7, no relative tolerance; "
                "column-count checked, unordered row multiset preserving duplicate counts, ordered only on "
                "explicit demand"
            ),
            "final_binding": "trace_learning.final_sql_result: strict binding to the executed final SQL span; failure is unknown, no intermediate fallback",
        },
        "coverage_note": (
            f"{origin_counts.get('REFERENCE_SQL', 0)} old reference + {origin_counts.get('REFERENCE_SQL_D2', 0)} D2 "
            f"reference + {origin_counts.get('native', 0)} native source records cover at most 23 + 23 learning "
            "questions; they are not independent samples."
        ),
        "d2_crosscheck": crosscheck,
        "origin_counts": origin_counts,
        "entries": entries,
    }

    out_dir = Path(options.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "oracle-d2.json").write_text(json.dumps(oracle_d2, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({
        "origin_counts": origin_counts,
        "d2_crosscheck_all_matched": all(c["oracle_matched"] for c in crosscheck),
        "manifest_sha256": digest_json(manifest),
    }, indent=1))


if __name__ == "__main__":
    main()
