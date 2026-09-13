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

"""POWE-145 step 0: preregistered numeric tolerance counterexamples.

Rule under test (coordinator dispatch 2026-09-13, fixed before any candidate
work): every numeric cell value - including numeric members inside dict cells
such as the trace encoder's {"decimal": "..."} wrapper - compares after
Decimal normalization with ABSOLUTE tolerance 5e-7 and NO relative tolerance.
Column-count equality, duplicate-preserving row multisets, explicit ordering
demands and strict final-SQL binding are unchanged.
"""

from decimal import Decimal

from powercontext_datus.trace_learning import ABS_TOL, cell_equal, rows_equal


def test_tolerance_constant_is_preregistered_absolute_only():
    assert ABS_TOL == Decimal("5e-7")


# --- dict cells: the trace encoder's decimal wrapper ------------------------


def test_decimal_wrapper_dict_matches_plain_string_number():
    # Last round's defect: DB DECIMALs arrive as {"decimal": "0.119904"} while
    # expectations carry "0.119904"; strict equality scored them wrong.
    assert cell_equal({"decimal": "0.119904"}, "0.119904")
    assert rows_equal([[{"decimal": "0.119904"}, 50]], [["0.119904", 50]])


def test_decimal_wrapper_dict_matches_float_and_int():
    assert cell_equal({"decimal": "1"}, 1.0)
    assert cell_equal({"decimal": "1.0000004"}, 1)


def test_dict_members_compare_numerically_and_keys_must_agree():
    assert cell_equal({"amount": "12.5"}, {"amount": 12.5000002})
    assert not cell_equal({"amount": "12.5"}, {"amount": 12.6})
    assert not cell_equal({"amount": "12.5"}, {"total": 12.5})


def test_generic_dict_cells_fall_back_to_recursive_exact_comparison():
    assert cell_equal({"a": [1, "x"]}, {"a": [1, "x"]})
    assert not cell_equal({"a": [1, "x"]}, {"a": [1, "y"]})


# --- numerically equal, different representations --------------------------


def test_equal_values_across_representations():
    assert cell_equal(1, "1.0")
    assert cell_equal("1.000", 1)
    assert cell_equal("1,234.5", 1234.5)
    assert rows_equal([["1", "2"]], [[1.0, 2.0000003]])


def test_boundary_exactly_at_tolerance_passes():
    assert abs(Decimal("1.0000005") - Decimal("1")) == ABS_TOL
    assert cell_equal(1, "1.0000005")


def test_above_tolerance_fails_without_relative_term():
    assert not cell_equal(1, "1.0000006")  # 6e-7 > 5e-7
    # No relative tolerance: a large magnitude must not widen the window
    # (the retired 1e-6 relative term accepted this difference).
    assert not cell_equal(123456789.123, "123456789.1239")
    # Exact Decimal arithmetic that float comparison collapsed:
    assert not cell_equal("99999999999.123456", Decimal("99999999999.123457"))
    assert cell_equal("99999999999.1234565", Decimal("99999999999.123457"))


def test_numeric_never_equals_non_numeric_and_bool_is_not_numeric():
    assert not cell_equal("1", "one")
    assert not cell_equal(1, None)
    assert not cell_equal(True, 1)
    assert cell_equal(True, True)


# --- unchanged structural rules --------------------------------------------


def test_column_count_mismatch_rejected():
    assert not rows_equal([[1, 2]], [[1]])
    assert not rows_equal([[1, 2], [3, 4, 5]], [[1, 2], [3, 4]])


def test_row_count_mismatch_rejected():
    assert not rows_equal([[1]], [[1], [1]])


def test_duplicate_multiset_preserved():
    assert not rows_equal([[1], [1], [2]], [[1], [2], [2]])
    assert rows_equal([[2], [1], [1]], [[1], [1], [2]])


def test_order_sensitivity_only_on_explicit_demand():
    assert rows_equal([["a", 10], ["b", 20]], [["b", 20], ["a", 10]])
    assert not rows_equal([["a", 10], ["b", 20]], [["b", 20], ["a", 10]], ordered=True)
    assert rows_equal([["a", 10], ["b", 20]], [["a", 10], ["b", 20]], ordered=True)


def test_mixed_decimal_wrapper_multiset_alignment():
    actual = [["x"], [{"decimal": "3"}], [{"decimal": "9"}]]
    expected = [["3"], ["9"], ["x"]]
    assert rows_equal(actual, expected)


def test_empty_results_equal_only_when_both_empty():
    assert rows_equal([], [])
    assert not rows_equal([], [[]])


# --- final binding: pyformat transport escaping (POWE-145 re-freeze) --------


def test_final_sql_result_collapses_pyformat_percent_escaping():
    from powercontext_datus.trace_learning import final_sql_result

    result = {
        "output": {"sql_query_final": "SELECT DATE_FORMAT(d,'%Y%m%d') FROM t"},
        "sql_results": [
            {"sql": "SELECT DATE_FORMAT(d,'%%Y%%m%%d') FROM t", "rows": [["20240101"]]},
        ],
    }
    sql, rows = final_sql_result(result)
    assert sql == "SELECT DATE_FORMAT(d,'%Y%m%d') FROM t"
    assert rows == [["20240101"]]
