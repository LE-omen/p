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

"""DESIGN-5 shared-prerequisite counterexamples (POWE-3-D5-PREP-1).

Covers the five required counterexample families plus the S_agent ledger:
column count, duplicate rows, row order, final-answer binding, missing trace.
"""

import pytest
from powercontext_datus.capture import agent_steps
from powercontext_datus.scoring import score_case, summarize_cases
from powercontext_datus.trace_learning import (
    evidence_from_run,
    final_sql_result,
    rows_equal,
    tables_of,
)

Q4_CTE_SQL = (
    "WITH t AS (\n"
    "  SELECT c.CustomerID, SUM(y.Consumption) AS total\n"
    "  FROM customers c\n"
    "  JOIN yearmonth y ON c.CustomerID = y.CustomerID\n"
    "  GROUP BY c.CustomerID\n"
    ")\n"
    "SELECT t.CustomerID FROM t ORDER BY total DESC LIMIT 1"
)


# --- tables_of: base-table grouping without CTE names ----------------------


def test_tables_of_excludes_cte_names_from_grouping():
    # Regression: the CTE name t used to split q4 into a singleton group
    # keyed by ('customers', 't', 'yearmonth').
    assert tables_of(Q4_CTE_SQL) == frozenset({"customers", "yearmonth"})


def test_tables_of_handles_nested_recursive_and_comma_lists():
    nested = "SELECT * FROM (WITH z AS (SELECT 1 FROM dual) SELECT * FROM z) x JOIN yearmonth y ON 1=1"
    assert tables_of(nested) == frozenset({"dual", "yearmonth"})
    recursive = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<5) SELECT * FROM r, gasstations"
    assert tables_of(recursive) == frozenset({"gasstations"})


def test_tables_of_keeps_qualified_and_backticked_base_tables():
    qualified = "SELECT * FROM information_schema.TABLES WHERE TABLE_SCHEMA = (SELECT database())"
    assert tables_of(qualified) == frozenset({"information_schema.tables"})
    backtick = "SELECT * FROM `birdbench`.`customers` JOIN `yearmonth` y ON 1=1"
    assert tables_of(backtick) == frozenset({"birdbench.customers", "yearmonth"})


# --- rows_equal: column count, duplicates, order ----------------------------


def test_rows_equal_rejects_column_count_mismatch():
    # Counterexample: zip truncation used to accept [[1, 2]] == [[1]].
    assert rows_equal([[1, 2]], [[1]]) is False
    assert rows_equal([[1]], [[1, 2]]) is False
    assert rows_equal([[1, 2], [3]], [[1, 2], [3, 4]]) is False


def test_rows_equal_multiset_preserves_duplicate_counts():
    assert rows_equal([[1], [2], [1]], [[1], [1], [2]]) is True
    assert rows_equal([[1], [1], [2]], [[1], [2], [2]]) is False
    assert rows_equal([["a"], ["a"], ["b"]], [["a"], ["b"], ["b"]]) is False


def test_rows_equal_order_modes():
    assert rows_equal([[1], [2]], [[2], [1]]) is True  # unordered default
    assert rows_equal([[1], [2]], [[2], [1]], ordered=True) is False
    assert rows_equal([[1], [2]], [[1], [2]], ordered=True) is True


def test_rows_equal_numeric_tolerance_is_retained():
    assert rows_equal([["1.0000001"]], [[1.0]]) is True
    assert rows_equal([[2, "3%"]], [[2.0, 3]]) is True
    assert rows_equal([["x"]], [[1.0]]) is False


# --- final_sql_result: strict binding to the executed final span ------------


def test_final_sql_result_never_falls_back_to_intermediate_execution():
    result = {
        "output": {"sql_query_final": "SELECT 42"},
        "sql_results": [{"sql": "SELECT 1", "rows": [[1]]}, {"sql": "SELECT 2", "rows": [[2]]}],
    }
    assert final_sql_result(result) is None


def test_final_sql_result_requires_nonempty_final_sql():
    result = {"output": {"sql_query_final": "  "}, "sql_results": [{"sql": "SELECT 1", "rows": [[1]]}]}
    assert final_sql_result(result) is None


def test_final_sql_result_binds_last_matching_execution():
    rows_late = [[9]]
    result = {
        "output": {"sql_query_final": "SELECT 1;"},
        "sql_results": [
            {"sql": "SELECT 1", "rows": [[1]]},
            {"sql": "SELECT 1 ;", "rows": rows_late},
        ],
    }
    assert final_sql_result(result) == ("SELECT 1 ;", rows_late)


def test_evidence_from_run_refuses_unbound_final_answer():
    result = {
        "output": {"sql_query_final": "SELECT 9"},
        "sql_results": [{"sql": "SELECT 1", "rows": [[1]]}],
        "steps": 2,
        "returncode": 0,
        "trace_issues": [],
        "question": "q",
    }
    evidence = evidence_from_run(
        qid=7, result=result, records=[], oracle_case={"expected": {"rows": [[1]]}}, manifest={}, final_verified=True
    )
    assert evidence is None


# --- agent_steps: S_agent reconciliation ------------------------------------


class Capture:
    def __init__(self):
        self.records = []

    def add(self, kind, **payload):
        record = {
            "kind": kind,
            "sequence": len(self.records) + 1,
            "run_id": "r",
            "task_id": "t",
            "attempt_id": "a",
            **payload,
        }
        self.records.append(record)
        return record

    def begin(self):
        self.add("capture_started")
        self.add("question_injected")

    def end(self):
        self.add("answer_submitted")
        self.add("capture_finished")

    def attempt(self, call_id, *, failed=False, with_wrapper=True, name="execute_sql"):
        self.add(
            "action_received",
            action={"role": "tool", "action_type": name, "status": "processing", "action_id": call_id},
        )
        operation_id = None
        if with_wrapper:
            operation_id = f"op-{call_id}"
            self.add(
                "operation_started",
                operation_id=operation_id,
                parent_id=None,
                name=name,
                inputs={},
                call_id=call_id,
                online=True,
            )
        self.add(
            "action_received",
            action={
                "role": "tool",
                "action_type": name,
                "status": "failed" if failed else "success",
                "action_id": "complete_" + call_id,
            },
        )
        if with_wrapper:
            self.add("operation_finished", operation_id=operation_id, status="success")


def test_agent_steps_counts_retries_and_failed_attempts():
    capture = Capture()
    capture.begin()
    capture.attempt("call-1", failed=True)
    capture.attempt("call-1-retry")
    capture.attempt("call-2")
    capture.end()
    ledger = agent_steps(capture.records)
    assert ledger["s_agent"] == 3
    assert ledger["failed_attempts"] == 1
    assert ledger["s_agent_issues"] == []


def test_agent_steps_counts_dispatch_without_wrapper():
    capture = Capture()
    capture.begin()
    capture.attempt("call-1", with_wrapper=False)  # malformed arguments never reached the wrapper
    capture.end()
    ledger = agent_steps(capture.records)
    assert ledger["s_agent"] == 1
    assert ledger["dispatches_without_wrapper"] == 1
    assert ledger["s_agent_issues"] == []


def test_agent_steps_marks_duplicate_and_orphan_ids_unknown():
    duplicate = Capture()
    duplicate.begin()
    duplicate.add(
        "action_received",
        action={"role": "tool", "action_type": "execute_sql", "status": "processing", "action_id": "call-1"},
    )
    duplicate.add(
        "action_received",
        action={"role": "tool", "action_type": "execute_sql", "status": "processing", "action_id": "call-1"},
    )
    duplicate.end()
    ledger = agent_steps(duplicate.records)
    assert ledger["s_agent"] is None
    assert "duplicate_attempt_call_id" in ledger["s_agent_issues"]

    orphan = Capture()
    orphan.begin()
    orphan.add(
        "operation_started",
        operation_id="op-1",
        parent_id=None,
        name="execute_sql",
        inputs={},
        call_id="ghost",
        online=True,
    )
    orphan.add("operation_finished", operation_id="op-1", status="success")
    orphan.end()
    ledger = agent_steps(orphan.records)
    assert ledger["s_agent"] is None
    assert "wrapper_without_attempt" in ledger["s_agent_issues"]


def test_agent_steps_requires_terminal_and_interval():
    missing_terminal = Capture()
    missing_terminal.begin()
    missing_terminal.add(
        "action_received",
        action={"role": "tool", "action_type": "execute_sql", "status": "processing", "action_id": "call-1"},
    )
    missing_terminal.add(
        "operation_started",
        operation_id="op-1",
        parent_id=None,
        name="execute_sql",
        inputs={},
        call_id="call-1",
        online=True,
    )
    missing_terminal.end()
    ledger = agent_steps(missing_terminal.records)
    assert ledger["s_agent"] is None
    assert "attempt_terminal_cardinality" in ledger["s_agent_issues"]

    no_interval = Capture()
    no_interval.add("capture_started")
    no_interval.add("capture_finished")
    assert agent_steps(no_interval.records)["s_agent"] is None


def test_agent_steps_secondary_ledger_splits_sql_kinds():
    capture = Capture()
    capture.begin()
    capture.attempt("call-dispatch", name="execute_sql")
    capture.attempt("call-introspect", name="describe_table")
    dispatch_op = next(
        r for r in capture.records if r["kind"] == "operation_started" and r["call_id"] == "call-dispatch"
    )
    introspect_op = next(
        r for r in capture.records if r["kind"] == "operation_started" and r["call_id"] == "call-introspect"
    )
    for span, sql, parent in [
        ("s1", "SELECT 1", dispatch_op["operation_id"]),
        ("s2", "USE `birdbench`", dispatch_op["operation_id"]),
        ("s3", "COMMIT", None),
        ("s4", "SHOW FULL TABLES FROM `birdbench`", introspect_op["operation_id"]),
        ("s5", "SELECT final_sql", None),
    ]:
        capture.add("sql_started", driver_span_id=span, sql=sql)
        capture.add("sql_link", driver_span_id=span, parent_id=parent, online=True, dialect="mysql", executemany=False)
    capture.add(
        "mysql_command_started",
        command_id="c1",
        online=True,
        driver_span_id=None,
        operation_id=dispatch_op["operation_id"],
    )
    capture.add("model_started", model="openai/test", input_sha256="0" * 64)
    capture.add("http_started", http_attempt_id="h1", method="POST", path="/v1", online=True)
    capture.add("model_finished", usage={"requests": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    capture.end()
    ledger = agent_steps(capture.records)
    assert ledger["sql_executions"] == 5
    assert ledger["task_sql_executions"] == 2  # dispatched execute_sql + fixed node re-execution
    assert ledger["dispatched_sql_executions"] == 1
    assert ledger["fixed_node_sql_executions"] == 1
    assert ledger["plumbing_sql_executions"] == 2
    assert ledger["introspection_sql_executions"] == 1  # SHOW under no execute_sql dispatch
    assert ledger["driver_events"] == 1
    assert ledger["model_requests"] == 1
    assert ledger["http_attempts"] == 1
    assert ledger["total_tokens"] == 15


# --- score_case: unified scorer unknown handling ----------------------------


def complete_result(final_sql, sql_results, *, output=None):
    return {
        "output": {"sql_query_final": final_sql, **(output or {})},
        "sql_results": sql_results,
        "steps": 2,
        "returncode": 0,
        "trace_issues": [],
        "question": "q",
    }


def base_capture():
    capture = Capture()
    capture.begin()
    capture.attempt("call-1", name="execute_sql")
    capture.attempt("call-2", name="execute_sql")
    capture.end()
    return capture


def test_score_case_missing_trace_is_unknown_not_incorrect():
    case = score_case(None, None, [[1]])
    assert case["correct"] is None
    assert case["unknown_reason"] == "missing_run_or_trace"
    assert case["in_budget"] is None
    assert summarize_cases([case])["unknown"] == 1


def test_score_case_binding_failure_is_unknown_never_intermediate():
    result = complete_result("SELECT 42", [{"sql": "SELECT 1", "rows": [[1]]}])
    case = score_case(result, base_capture().records, [[1]])
    assert case["correct"] is None
    assert case["unknown_reason"] == "final_binding_failed"


def test_score_case_failed_run_without_output_is_unknown():
    result = {"output": None, "returncode": 1, "trace_issues": ["interrupted"]}
    case = score_case(result, base_capture().records, [[1]])
    assert case["correct"] is None
    assert case["unknown_reason"] == "run_failed"


def test_score_case_correctness_and_budget_use_s_agent():
    records = base_capture().records
    ok = score_case(complete_result("SELECT 1", [{"sql": "SELECT 1", "rows": [[1]]}]), records, [[1]])
    assert ok["correct"] is True
    assert ok["s_agent"] == 2
    assert ok["in_budget"] is True

    wrong_rows = score_case(complete_result("SELECT 1", [{"sql": "SELECT 1", "rows": [[2]]}]), records, [[1]])
    assert wrong_rows["correct"] is False
    assert wrong_rows["in_budget"] is False

    capture_three = Capture()
    capture_three.begin()
    capture_three.attempt("call-1", name="execute_sql")
    capture_three.attempt("call-2", name="execute_sql")
    capture_three.attempt("call-3", name="execute_sql")
    capture_three.end()
    slow = score_case(complete_result("SELECT 1", [{"sql": "SELECT 1", "rows": [[1]]}]), capture_three.records, [[1]])
    assert slow["correct"] is True
    assert slow["s_agent"] == 3
    assert slow["in_budget"] is False

    summary = summarize_cases([ok, wrong_rows, slow])
    assert summary["correct"] == 2
    assert summary["incorrect"] == 1
    assert summary["j_agent_correct_and_within_2"] == 1


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ([[1, 2]], [[1]]),  # column count
        ([[1], [1], [2]], [[1], [2], [2]]),  # duplicate rows
        ([[1], [2], [3]], [[3], [2], [1]]),  # order only checked when explicitly required
    ],
)
def test_counterexample_families_rejected(actual, expected):
    assert rows_equal(actual, expected, ordered=True) is False
