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

_IDENT = r"`[^`]+`|[A-Za-z_][A-Za-z0-9_]*"
_IDENT_GROUP = rf"(?:{_IDENT})"
_TABLE_PATTERN = re.compile(
    rf"\b(?:from|join)\s+({_IDENT_GROUP}(?:\s*\.\s*{_IDENT_GROUP})*(?:\s*,\s*{_IDENT_GROUP}(?:\s*\.\s*{_IDENT_GROUP})*)*)",
    re.IGNORECASE,
)
_IDENTIFIER_AT = re.compile(rf"\s*({_IDENT})")
_PAREN_AT = re.compile(r"\s*\(")
_AS_AT = re.compile(r"\s*as\b", re.IGNORECASE)
_COMMA_AT = re.compile(r"\s*,")
_MATERIALIZED_AT = re.compile(r"\s*(?:not\s+)?materialized\b", re.IGNORECASE)
_WITH_AT = re.compile(r"\bwith\b", re.IGNORECASE)


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


def rows_equal(actual: list[list[Any]], expected: list[list[Any]], *, ordered: bool = False) -> bool:
    """Row-multiset equality with numeric tolerance (BIRD-style value comparison).

    Column counts must agree on every row (zip truncation never accepts a
    wider row against a narrower one). Unordered comparison is a row
    multiset that preserves duplicate counts; pass ordered=True only when the
    question explicitly requires a specific row order.
    """
    if len(actual) != len(expected):
        return False
    if not actual:
        return True
    if any(
        not isinstance(row, list) or any(isinstance(cell, (dict, list)) for cell in row) for row in [*actual, *expected]
    ):
        return actual == expected
    width = len(actual[0])
    if len(expected[0]) != width or any(len(row) != width for row in [*actual, *expected]):
        return False
    if ordered:
        return all(
            cell_equal(x, y) for ra, rb in zip(actual, expected, strict=True) for x, y in zip(ra, rb, strict=True)
        )
    keyed_actual = sorted((tuple(numify(c) for c in row) for row in actual), key=repr)
    keyed_expected = sorted((tuple(numify(c) for c in row) for row in expected), key=repr)
    return all(
        cell_equal(x, y)
        for ra, rb in zip(keyed_actual, keyed_expected, strict=True)
        for x, y in zip(ra, rb, strict=True)
    )


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
    """Bind the final answer to the actually executed span of the final SQL.

    Strict binding (DESIGN-5 D5-METRIC): the scored rows must come from the
    last execution whose SQL matches ``sql_query_final``. Any binding failure
    (missing final SQL, or no matching execution) returns None - the caller
    must record unknown instead of falling back to an intermediate execution.
    """
    output = result.get("output") or {}
    final_sql = (output.get("sql_query_final") or "").strip()
    if not final_sql:
        return None

    def _normalized(sql: str) -> str:
        return sql.strip().rstrip(";").strip()

    matching = [
        entry for entry in executed_sqls(result) if _normalized(entry.get("sql") or "") == _normalized(final_sql)
    ]
    if not matching:
        return None
    entry = matching[-1]
    return entry.get("sql") or "", entry.get("rows") or []


def matching_executions(result: dict[str, Any], expected_rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Every executed SQL whose result rows equal the expected rows."""
    return [entry for entry in executed_sqls(result) if rows_equal(entry.get("rows") or [], expected_rows)]


def _unquote(identifier: str) -> str:
    return (
        identifier[1:-1]
        if len(identifier) > 1 and identifier.startswith("`") and identifier.endswith("`")
        else identifier
    )


def _skip_balanced_parens(sql: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _collect_cte_definitions(sql: str, pos: int, names: set[str]) -> None:
    """Collect ``name [ (columns) ] AS [NOT] [MATERIALIZED] ( ... )`` chains from pos."""
    parsed = _IDENTIFIER_AT.match(sql, pos)
    if parsed is not None and _unquote(parsed.group(1)).lower() == "recursive":
        pos = parsed.end()
        parsed = _IDENTIFIER_AT.match(sql, pos)
    while parsed is not None:
        name, pos = _unquote(parsed.group(1)), parsed.end()
        columns = _PAREN_AT.match(sql, pos)
        if columns is not None:
            end = _skip_balanced_parens(sql, columns.end() - 1)
            if end is None:
                return
            pos = end
        as_keyword = _AS_AT.match(sql, pos)
        if as_keyword is None:
            return
        pos = as_keyword.end()
        modifier = _MATERIALIZED_AT.match(sql, pos)
        if modifier is not None:
            pos = modifier.end()
        body = _PAREN_AT.match(sql, pos)
        if body is None:
            return
        end = _skip_balanced_parens(sql, body.end() - 1)
        if end is None:
            return
        names.add(name.lower())
        pos = end
        comma = _COMMA_AT.match(sql, pos)
        if comma is None:
            return
        pos = comma.end()
        parsed = _IDENTIFIER_AT.match(sql, pos)


def _cte_names(sql: str) -> frozenset[str]:
    """Every name defined by a ``WITH name AS ( ...)`` clause, nested included.

    Only fully parsed ``name [ (columns) ] AS [NOT] [MATERIALIZED] ( ... )``
    sequences count, so stray ``with`` words never steal a real table name.
    Derived-table aliases cannot enter ``_TABLE_PATTERN`` at all (a
    parenthesis, not an identifier, follows FROM/JOIN), so CTE names are the
    only aliases that must be excluded here.
    """
    names: set[str] = set()
    for keyword in _WITH_AT.finditer(sql):
        _collect_cte_definitions(sql, keyword.end(), names)
    return frozenset(names)


def tables_of(sql: str) -> frozenset[str]:
    """Base tables referenced by FROM/JOIN, grouped by real base tables only.

    CTE names defined in the statement (including nested WITH clauses) are
    excluded, so ``WITH t AS (SELECT ... FROM customers) SELECT * FROM t``
    groups under ``customers`` instead of splitting into a singleton group
    keyed by the CTE name.
    """
    ctes = _cte_names(sql)
    qualified = re.compile(rf"{_IDENT_GROUP}(?:\s*\.\s*{_IDENT_GROUP})*")
    tables: set[str] = set()
    for match in _TABLE_PATTERN.finditer(sql):
        for name_match in qualified.finditer(match.group(1)):
            name = ".".join(_unquote(part) for part in re.findall(_IDENT, name_match.group(0))).lower()
            if name.split(".")[-1] not in ctes:
                tables.add(name)
    return frozenset(tables)


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
