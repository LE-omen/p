"""POWE-137 ARM-B: unified scoring of a run set + evaluator-provided D feedback summary.

Scores each qNXX run dir with the frozen unified scorer (scoring.score_case,
ordered flag from the D contracts / R oracle) and, for D sets, emits the
failure-feedback digest the B generator is allowed to read: per failed or
unknown question - family, agent final SQL, mismatch shape (row counts,
columns, first differing cells), reference SQL (released D package material),
S_agent, and a mechanical error-mode classification.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent / "powercontext"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "integrations" / "datus" / "src"))

from powercontext_datus.scoring import score_case, summarize_cases  # noqa: E402
from powercontext_datus.trace_learning import rows_equal  # noqa: E402


def decode_transport_rows(result: dict) -> dict:
    """Apply the frozen bridge decode policy (capture.table_from_json) to recorded rows.

    The trace encodes DECIMAL cells as {"decimal": "..."} (capture.encode_cell).
    Decoding follows the frozen e1_build.normalize_cell policy (Decimal -> float),
    so agent-side decimals meet the same numeric-tolerance comparison as the
    E1 expected rows. Evaluator-side transport decoding, not a comparator change.
    """

    def cell(v):
        if isinstance(v, dict) and set(v) == {"decimal"}:
            return float(v["decimal"])
        return v

    for entry in result.get("sql_results") or []:
        if isinstance(entry.get("rows"), list):
            entry["rows"] = [[cell(v) for v in row] if isinstance(row, list) else row for row in entry["rows"]]
    return result


def load_records(qdir: Path) -> list[dict]:
    lines = (qdir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if (qdir / "trace.jsonl").exists() else []
    return [json.loads(x) for x in lines if x.strip()]


def classify(expected_rows, actual_rows, columns_ok, row_count_ok, ordered_ok, result, case):
    if case["correct"] is None:
        return f"unknown:{case.get('unknown_reason')}"
    if not columns_ok:
        return "column_shape_mismatch"
    if not row_count_ok:
        return "row_count_mismatch"
    if ordered_ok is False:
        return "row_order_mismatch"
    if result.get("s_agent_issues"):
        return "value_mismatch_with_step_issues"
    return "value_mismatch"


def diff_shape(expected_rows, actual_rows):
    row_count_ok = len(expected_rows) == len(actual_rows)
    columns_ok = bool(expected_rows) and bool(actual_rows) and len(expected_rows[0]) == len(actual_rows[0])
    first_diff = None
    if columns_ok:
        for i, (re_, ra) in enumerate(zip(expected_rows, actual_rows)):
            for j, (ce, ca) in enumerate(zip(re_, ra)):
                if not rows_equal([[ce]], [[ca]]):
                    first_diff = {"row": i, "col": j, "expected": str(ce)[:80], "actual": str(ca)[:80]}
                    break
            if first_diff:
                break
    return row_count_ok, columns_ok, first_diff


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cand", required=True)
    parser.add_argument("--set", required=True, choices=["d5", "d10", "d23", "r1", "r2", "r3", "d5-base"])
    parser.add_argument("--out", required=True)
    options = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    workdir = root.parent
    runs_root = root / "runs" / options.cand / options.set
    if options.set.startswith("r"):
        oracle = json.loads((root / "records" / "r-oracle.json").read_text(encoding="utf-8"))["expected"]
        meta = {str(v["qid"]): v for v in oracle.values()}
    else:
        root = workdir / "arm-b-data" / "d-package"
        qs = json.loads((root / "questions.json").read_text(encoding="utf-8"))
        qs = qs["questions"] if isinstance(qs, dict) else qs
        dm = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        wanted = set(dm["screen_5"] if options.set.startswith("d5") else dm["screen_10"] if options.set.startswith("d10") else dm["question_ids"])
        qs = [q for q in qs if q["id"] in wanted]
        meta = {}
        for q in qs:
            o = json.loads((root / "oracle" / f"{q['id']}.json").read_text(encoding="utf-8"))
            meta[q["id"]] = {
                "id": q["id"],
                "question": q["question"],
                "family": q["semantic_family"],
                "columns": o["result"]["columns"],
                "rows": o["result"]["rows"],
                "reference_sql": (root / "reference_sql" / f"{q['id']}.sql").read_text(encoding="utf-8").strip(),
                # D contracts: every D question explicitly demands its stated order
                "ordered": True,
            }

    def sort_key(kv):
        key = kv[0]
        return int(key[1:]) if key.startswith("N") else int(key)

    cases, feedback = [], []
    for key, m in sorted(meta.items(), key=sort_key):
        qdir = runs_root / f"q{key}"
        if not (qdir / "result.json").exists():
            cases.append({"qid": key, "correct": None, "unknown_reason": "missing_run_or_trace", "s_agent": None, "in_budget": None})
            continue
        result = decode_transport_rows(json.loads((qdir / "result.json").read_text(encoding="utf-8")))
        records = load_records(qdir)
        case = score_case(result, records, m["rows"], ordered=bool(m.get("ordered")))
        case["qid"] = key
        case["question"] = m["question"][:110]
        cases.append(case)
        if options.set.startswith("d") and case["correct"] is not True:
            chosen_rows = None
            if case.get("final_sql"):
                for entry in result.get("sql_results") or []:
                    if entry.get("sql", "").strip() == case["final_sql"].strip():
                        chosen_rows = entry.get("rows")
                        break
            rc_ok, col_ok, first_diff = diff_shape(m["rows"], chosen_rows or [])
            ordered_ok = None
            if chosen_rows and rows_equal(chosen_rows, m["rows"]) and not rows_equal(chosen_rows, m["rows"], ordered=True):
                ordered_ok = False
            feedback.append({
                "qid": m.get("id", key),
                "family": m.get("family"),
                "question": m["question"],
                "correct": case["correct"],
                "s_agent": case.get("s_agent"),
                "agent_final_sql": case.get("final_sql"),
                "agent_rows": chosen_rows,
                "expected_columns": m["columns"],
                "expected_row_count": len(m["rows"]),
                "reference_sql": m.get("reference_sql"),
                "row_count_ok": rc_ok,
                "columns_ok": col_ok,
                "first_diff": first_diff,
                "error_mode": classify(m["rows"], chosen_rows or [], col_ok, rc_ok, ordered_ok, result, case),
            })

    summary = summarize_cases(cases)
    payload = {
        "cand": options.cand,
        "set": options.set,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cases": cases,
        "feedback": feedback,
    }
    Path(options.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"{options.cand}/{options.set}: C={summary['correct']}/{summary['total']} "
          f"J={summary['j_agent_correct_and_within_2']} unknown={summary['unknown']} "
          f"meanS={summary['mean_s_agent']} feedback_items={len(feedback)}")


if __name__ == "__main__":
    sys.exit(main())
