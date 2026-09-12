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

"""Build the DESIGN-5 E1 evidence pool manifest (POWE-3-D5-PREP-1).

E1 adds, on top of the preserved E0 native evidence (historical 8 items):

- REFERENCE_SQL: the authorized QA judgment CSV's example_sql for the
  original learning half (even CSV rows 0-44), each executed independently
  under a read-only, SELECT-only controlled connection.
- negative: for learning-half runs with a complete trace and a bound final
  answer whose rows differ from the reference rows, an "agent SQL vs
  reference SQL" negative control entry.
- unverified: bookkeeping entries for runs whose evidence cannot be
  mechanically certified (incomplete trace, binding failure, reference
  execution error); they never become generation input.

The old validation half (odd rows) never enters E1. The manifest keeps a
per-origin ledger: REFERENCE_SQL + native records describe at most the 23
learning questions, not independent samples.

Runs in the main repository environment; the only secret is DB_PASSWORD
from the environment.

  PYTHONPATH=integrations/datus/src uv run --frozen python integrations/datus/e1_build.py \
      --csv JUDGMENT_CSV --runs-root NATIVE_RUNS --selection SELECTION_JSON --out-dir integrations/datus/e1
"""

# ruff: noqa: TRY003 - bounded validation errors are part of this script's diagnostics.
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "datus" / "src"))

from powercontext_datus.freeze import digest_json  # noqa: E402
from powercontext_datus.learning import LearningEvidence  # noqa: E402
from powercontext_datus.trace_learning import (  # noqa: E402
    final_sql_result,
    matching_executions,
    rows_equal,
    run_is_complete,
)

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
    from decimal import Decimal

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


def execute_reference(rows_by_qid: dict[int, str], db: dict) -> dict[int, dict]:
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
    results: dict[int, dict] = {}
    try:
        with connection.cursor() as cursor:
            for qid, sql in rows_by_qid.items():
                assert_readonly_select(sql)
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                data = [[normalize_cell(v) for v in row] for row in cursor.fetchmany(ROW_CAP + 1)]
                if len(data) > ROW_CAP:
                    raise ValueError(f"reference q{qid} exceeded row cap {ROW_CAP}")
                results[qid] = {"columns": columns, "rows": data}
    finally:
        connection.close()
    return results


def load_run(run_dir: Path) -> tuple[dict, list[dict], dict]:
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    manifest = json.loads((run_dir / "payload.json").read_text(encoding="utf-8"))
    return result, records, manifest


def reference_entry(qid: int, question: str, sql: str, executed: dict) -> dict:
    evidence = LearningEvidence(
        sample_id=f"reference-q{qid}",
        question=question,
        sql=sql,
        result_digest=digest_json(executed["rows"]),
        native_receipt_digest=digest_json({
            "origin": "REFERENCE_SQL",
            "qid": qid,
            "columns": executed["columns"],
            "row_count": len(executed["rows"]),
        }),
        source_manifest_digest=digest_json({
            "source": "judgment_csv",
            "qid": qid,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        }),
        lesson=(
            "Reference SQL from the authorized QA judgment CSV, independently executed against the "
            "read-only database under a SELECT-only controlled connection; its result rows are the "
            "verified reference for this question (origin=REFERENCE_SQL)."
        ),
    )
    return {
        "origin": "REFERENCE_SQL",
        "qid": qid,
        **vars(evidence),
        "columns": executed["columns"],
        "row_count": len(executed["rows"]),
    }


def negative_entry(qid: int, result: dict, chosen: tuple[str, list], reference_rows: list[list]) -> dict:
    sql, rows = chosen
    return {
        "origin": "negative",
        "qid": qid,
        "sample_id": f"negative-q{qid}",
        "question": result.get("question") or "",
        "sql": sql,
        "agent_rows_digest": digest_json(rows),
        "reference_rows_digest": digest_json(reference_rows),
        "agent_row_count": len(rows),
        "reference_row_count": len(reference_rows),
        "lesson": (
            "Negative control: the agent's bound final SQL for this question produced rows different "
            "from the independently executed reference SQL; this pattern is a counter-example, not "
            "reusable guidance (origin=negative)."
        ),
    }


def build_native_e0(selection: dict, runs_root: Path, reference: dict[int, dict], csv_rows: list[dict]) -> list[dict]:
    """Re-verify the preserved E0 items under the unified comparator."""
    from powercontext_datus.trace_learning import evidence_from_run

    native_e0: list[dict] = []
    for item in selection["items"]:
        qid = item["qid"]
        result, records, manifest = load_run(runs_root / f"q{qid}")
        sql_entry = None
        if item.get("kind") == "intermediate":
            hits = matching_executions(result, reference[qid]["rows"])
            if not hits:
                raise SystemExit(f"E0 q{qid}: no oracle-matching executed SQL under the unified comparator")
            sql_entry = hits[0]
        evidence = evidence_from_run(
            qid=qid,
            result=result,
            records=records,
            oracle_case={"expected": reference[qid], "question": csv_rows[qid]["question"]},
            manifest=manifest,
            sql_entry=sql_entry,
            final_verified=item.get("kind", "final") == "final",
        )
        if evidence is None:
            raise SystemExit(f"E0 q{qid}: failed mechanical verification under the fixed scorer")
        native_e0.append({"origin": "native", "qid": qid, "kind": item.get("kind", "final"), **vars(evidence)})
    return native_e0


def classify_learning_runs(
    runs_root: Path, learning_qids: list[int], reference: dict[int, dict], selected_qids: set[int]
) -> tuple[list[dict], list[int]]:
    """Negative controls and unverified bookkeeping for every learning-half run."""
    entries: list[dict] = []
    eligible_unselected: list[int] = []
    for qid in learning_qids:
        result, _, _ = load_run(runs_root / f"q{qid}")
        reference_rows = reference[qid]["rows"]
        if not run_is_complete(result):
            entries.append({
                "origin": "unverified",
                "qid": qid,
                "sample_id": f"unverified-q{qid}",
                "reason": "incomplete_trace",
                "trace_issues": result.get("trace_issues") or [],
            })
            continue
        chosen = final_sql_result(result)
        if chosen is None:
            entries.append({
                "origin": "unverified",
                "qid": qid,
                "sample_id": f"unverified-q{qid}",
                "reason": "final_binding_failed",
            })
        elif rows_equal(chosen[1], reference_rows):
            if qid not in selected_qids:
                eligible_unselected.append(qid)
        else:
            entries.append(negative_entry(qid, result, chosen, reference_rows))
    return entries, eligible_unselected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument(
        "--runs-root",
        required=True,
        help="frozen native learning-half runs (result.json/trace.jsonl/payload.json per qN)",
    )
    parser.add_argument("--selection", required=True, help="E0 selection JSON (criteria + items)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--db-host", default="t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="mock_data_readonly")
    parser.add_argument("--db-name", default="birdbench")
    options = parser.parse_args()

    csv_path = Path(options.csv)
    with csv_path.open(encoding="utf-8-sig") as fh:
        csv_rows = list(csv.DictReader(fh))
    learning_qids = list(range(0, len(csv_rows), 2))
    validation_qids = list(range(1, len(csv_rows), 2))
    if len(learning_qids) != 23 or len(validation_qids) != 23:
        raise SystemExit(
            f"expected the original 23/23 learning/validation split, got {len(learning_qids)}/{len(validation_qids)}"
        )

    db = {"host": options.db_host, "port": options.db_port, "user": options.db_user, "database": options.db_name}
    reference_sql = {qid: csv_rows[qid]["example_sql"].strip().rstrip(";") for qid in learning_qids}
    reference = execute_reference(reference_sql, db)
    expected_rows = {str(qid): reference[qid] for qid in learning_qids}

    runs_root = Path(options.runs_root)
    selection = json.loads(Path(options.selection).read_text(encoding="utf-8"))
    native_e0 = build_native_e0(selection, runs_root, reference, csv_rows)
    tail_entries, eligible_unselected = classify_learning_runs(
        runs_root, learning_qids, reference, {item["qid"] for item in selection["items"]}
    )

    reference_entries = [
        reference_entry(qid, csv_rows[qid]["question"], reference_sql[qid], reference[qid]) for qid in learning_qids
    ]
    entries = reference_entries + native_e0 + tail_entries

    origin_counts: dict[str, int] = {}
    for entry in entries:
        origin_counts[entry["origin"]] = origin_counts.get(entry["origin"], 0) + 1

    manifest = {
        "e1_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "base_head": os.environ.get("E1_BASE_HEAD", ""),
        "csv": {"path": str(csv_path), "sha256": sha256_file(csv_path)},
        "db": {k: db[k] for k in ("host", "port", "user", "database")},
        "row_cap": ROW_CAP,
        "learning_qids": learning_qids,
        "validation_qids_excluded": validation_qids,
        "rules": {
            "origin": {
                "REFERENCE_SQL": "judgment-CSV example_sql for the learning half, independently executed read-only/SELECT-only",
                "native": "preserved E0 evidence (8 historical items), re-verified under the unified comparator",
                "negative": "complete run with bound final SQL whose rows differ from the reference rows",
                "unverified": "cannot be mechanically certified (incomplete trace or binding failure); never generation input",
            },
            "comparator": "trace_learning.rows_equal: column-count checked, unordered row multiset preserving duplicate counts, ordered only on explicit demand",
            "final_binding": "trace_learning.final_sql_result: strict binding to the executed final SQL span; failure is unknown, no intermediate fallback",
        },
        "coverage_note": (
            f"{origin_counts.get('REFERENCE_SQL', 0)} reference + {origin_counts.get('native', 0)} native source records "
            f"cover at most {len(learning_qids)} learning questions; they are not independent samples."
        ),
        "native_eligible_unselected": eligible_unselected,
        "origin_counts": origin_counts,
        "entries": entries,
    }

    out_dir = Path(options.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_payload = {
        "note": "reference rows from the independent controlled execution; scorer input for the learning half",
        "expected": expected_rows,
    }
    (out_dir / "expected-rows.json").write_text(
        json.dumps(expected_payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    manifest["expected_rows_sha256"] = sha256_file(out_dir / "expected-rows.json")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "origin_counts": origin_counts,
                "native_eligible_unselected": eligible_unselected,
                "manifest": str(out_dir / "manifest.json"),
                "manifest_sha256": sha256_file(out_dir / "manifest.json"),
                "expected_rows_sha256": manifest["expected_rows_sha256"],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
