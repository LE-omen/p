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

"""Unified DESIGN-5 case scorer: one comparator for selection and final runs.

Shared rules (POWE-3-DESIGN-5 D5-METRIC):

- Correctness compares the rows of the *executed final SQL span* against the
  expected rows with ``rows_equal`` (column-count checked; unordered by
  default with duplicate counts preserved; ``ordered=True`` only for
  questions that explicitly demand an order).
- Primary step count is S_agent (online model tool-call attempts) from
  ``agent_steps``; the ledger's secondary counts (task SQL executions,
  fixed-node executions, driver events, model requests/tokens) travel with
  every case record.
- Unknowns never disappear: a missing run/trace, a failed run, or a final
  answer that cannot be bound to its executed SQL span scores ``correct=None``
  (unknown) - there is no fallback to an intermediate execution, and unknown
  stays in the scoring denominator.
"""

from __future__ import annotations

from typing import Any

from powercontext_datus.capture import agent_steps
from powercontext_datus.trace_learning import final_sql_result, rows_equal


def score_case(
    result: dict[str, Any] | None,
    records: list[dict[str, Any]] | None,
    expected_rows: list[list[Any]],
    *,
    ordered: bool = False,
) -> dict[str, Any]:
    """Score one question run under the unified rules.

    Returns a case record with ``correct`` (True/False/None-unknown),
    ``unknown_reason`` (when correct is None), ``s_agent``/``in_budget``
    (None while the underlying value is unknown), and the full secondary
    ``ledger`` from :func:`powercontext_datus.capture.agent_steps`.
    """
    ledger = agent_steps([] if records is None else records)
    case: dict[str, Any] = {
        "correct": None,
        "unknown_reason": "missing_run_or_trace",
        "s_agent": ledger["s_agent"],
        "in_budget": None,
        "final_sql": None,
        "ledger": ledger,
    }
    if result is None or records is None:
        return case
    case["returncode"] = result.get("returncode")
    if not result.get("output"):
        case["unknown_reason"] = "run_failed"
        return case
    chosen = final_sql_result(result)
    if chosen is None:
        case["unknown_reason"] = "final_binding_failed"
        return case
    sql, rows = chosen
    correct = rows_equal(rows, expected_rows, ordered=ordered)
    case.update(correct=correct, unknown_reason=None, final_sql=sql)
    if ledger["s_agent"] is None:
        case["in_budget"] = None
    else:
        case["in_budget"] = bool(correct) and ledger["s_agent"] <= 2
    return case


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scored cases: unknowns stay in the denominator."""
    total = len(cases)
    known_steps = [c["s_agent"] for c in cases if c.get("s_agent") is not None]
    return {
        "total": total,
        "correct": sum(1 for c in cases if c.get("correct") is True),
        "incorrect": sum(1 for c in cases if c.get("correct") is False),
        "unknown": sum(1 for c in cases if c.get("correct") is None),
        "unknown_reasons": sorted({c.get("unknown_reason") for c in cases if c.get("correct") is None}),
        "j_agent_correct_and_within_2": sum(1 for c in cases if c.get("in_budget") is True),
        "s_agent_distribution": {
            str(step): sum(1 for s in known_steps if s == step) for step in sorted(set(known_steps))
        },
        "s_agent_unknown": sum(1 for c in cases if c.get("s_agent") is None),
        "mean_s_agent": round(sum(known_steps) / len(known_steps), 2) if known_steps else None,
    }


__all__ = ["score_case", "summarize_cases"]
