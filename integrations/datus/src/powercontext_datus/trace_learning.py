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

"""Mechanical conversion of frozen native Datus run records into learning evidence.

Only evaluator-verified executions become evidence. The caller supplies the
expected rows; this module compares executed SQL results value-by-value before
any LearningEvidence is constructed. It never authors Skill content - managed
Skills are generated server-side from the captured evidence.
"""

from __future__ import annotations

import re
from typing import Any

from powercontext_datus.freeze import digest_json
from powercontext_datus.learning import LearningEvidence

REL_TOL = 1e-6
ABS_TOL = 1e-6

_TABLE_PATTERN = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)


def numify(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "").replace(",", "")
        try:
            return float(text)
        except ValueError:
            return value.strip()
    return value


def cell_equal(left: Any, right: Any) -> bool:
    left, right = numify(left), numify(right)
    if isinstance(left, float) and isinstance(right, float):
        return abs(left - right) <= max(ABS_TOL, REL_TOL * max(abs(left), abs(right)))
    return left == right


def rows_equal(actual: list[list[Any]], expected: list[list[Any]]) -> bool:
    """Row-multiset equality with numeric tolerance (BIRD-style value comparison)."""
    if len(actual) != len(expected):
        return False
    if not actual:
        return True
    if any(not isinstance(row, list) or any(isinstance(cell, (dict, list)) for cell in row)
           for row in [*actual, *expected]):
        return actual == expected
    if len(actual[0]) == 1 and len(expected[0]) == 1:
        return all(cell_equal(a[0], b[0]) for a, b in zip(actual, expected))
    keyed_actual = sorted((tuple(numify(c) for c in row) for row in actual), key=repr)
    keyed_expected = sorted((tuple(numify(c) for c in row) for row in expected), key=repr)
    return all(cell_equal(x, y) for ra, rb in zip(keyed_actual, keyed_expected) for x, y in zip(ra, rb))


def run_is_complete(result: dict[str, Any]) -> bool:
    return (
        not result.get("trace_issues")
        and isinstance(result.get("steps"), int)
        and result.get("returncode") == 0
        and bool(result.get("output"))
    )


def executed_sqls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in (result.get("sql_results") or []) if entry.get("sql")]


def final_sql_result(result: dict[str, Any]) -> tuple[str, list[list[Any]]] | None:
    """Select the final SQL and its rows exactly as the evaluator scores them."""
    output = result.get("output") or {}
    final_sql = (output.get("sql_query_final") or "").strip()
    entries = executed_sqls(result)
    if not entries:
        return None
    matching = [e for e in entries if (e.get("sql") or "").strip().rstrip(";") == final_sql.rstrip(";")]
    entry = matching[-1] if matching else entries[-1]
    return entry.get("sql") or "", entry.get("rows") or []


def matching_executions(result: dict[str, Any], expected_rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Every executed SQL whose result rows equal the expected rows."""
    return [entry for entry in executed_sqls(result) if rows_equal(entry.get("rows") or [], expected_rows)]


def tables_of(sql: str) -> frozenset[str]:
    return frozenset(match.lower() for match in _TABLE_PATTERN.findall(sql))


def evidence_from_run(
    *,
    qid: int,
    result: dict[str, Any],
    records: list[dict[str, Any]],
    oracle_case: dict[str, Any],
    manifest: dict[str, Any],
    sql_entry: dict[str, Any] | None = None,
    final_verified: bool = False,
) -> LearningEvidence | None:
    """Build LearningEvidence from one run when its evidence is mechanically verified.

    final_verified: the run's final answer matched the oracle (requires a complete run).
    sql_entry: an executed SQL entry verified against the oracle for an otherwise
      failed run; only admitted for complete runs.
    """
    expected = (oracle_case.get("expected") or {}).get("rows") or []
    if not run_is_complete(result):
        return None
    steps = result["steps"]
    question = result.get("question") or oracle_case.get("question") or ""
    if final_verified:
        chosen = final_sql_result(result)
        if chosen is None or not rows_equal(chosen[1], expected):
            return None
        sql, rows = chosen
        lesson = (
            f"Independent native execution answered this question correctly in {steps} tool calls; "
            "the final SQL and its result rows were verified against the evaluator oracle."
        )
        sample_id = f"native-q{qid}-final"
    elif sql_entry is not None:
        sql = (sql_entry.get("sql") or "").strip()
        rows = sql_entry.get("rows") or []
        if not sql or not rows_equal(rows, expected):
            return None
        lesson = (
            f"Independent native execution of this question failed overall in {steps} tool calls, but one "
            "executed SQL produced rows equal to the evaluator oracle result for this question."
        )
        sample_id = f"native-q{qid}-sql"
    else:
        return None
    return LearningEvidence(
        sample_id=sample_id,
        question=question,
        sql=sql,
        result_digest=digest_json(rows),
        native_receipt_digest=digest_json({"records": records, "result": result}),
        source_manifest_digest=digest_json(manifest),
        lesson=lesson,
    )


__all__ = [
    "cell_equal",
    "evidence_from_run",
    "executed_sqls",
    "final_sql_result",
    "matching_executions",
    "numify",
    "rows_equal",
    "run_is_complete",
    "tables_of",
]
