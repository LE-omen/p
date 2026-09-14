"""POWE-166 zero-model fixture battery for the A-fix (r5patch.install).

Runs the REAL datus runtime parser (powe-152 datus-runtime venv) on saved
final-response texts from H4 traces plus synthetic edge cases, BEFORE and
AFTER installing the recovery patch. No model call, no DB access.

Battery (ids match report):
  F1 real h41-S4/qV4-36 r1  : set `{5,8,9,16}` in prose + legal {"sql":...} envelope -> must yield dict with sql
  F2 real h43-S4/qV4-36 r3  : same family                              -> must yield dict with sql
  F3 real h42-H0/qV4-36 r2  : set `{5, 8, 9, 18}` + envelope           -> must yield dict with sql
  F4 real h42-S4/qV4-36 r2  : leading `n{"columns":...}` + fenced prose, NO sql envelope -> dict without sql (effective sql=None, context path governs; baseline None)
  F5 real h42-S4/qV4-31 r2  : prose only, no JSON object                -> stays None (failure preserved)
  F6 pure JSON object                                                    -> unchanged dict
  F7 ```json fenced object                                              -> unchanged dict
  F8 SQL string containing braces inside a valid envelope               -> dict with sql intact (string-aware scan)
  F9 two valid objects, first without sql                               -> first valid dict (no schema preference; deterministic)
  F10 truncated envelope (no closing brace)                             -> stays failure-ish (baseline repair behavior unchanged; no completion invented)

Verdict rule printed per case: BASELINE vs PATCHED extraction, plus the
requirement classification (MUST_FIX / MUST_HOLD).
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FIXTURES = HERE.parent / "fixtures"

F6 = '{"sql": "SELECT 1", "output": "x"}'
F7 = "```json\n" + F6 + "\n```"
F8 = json.dumps({"sql": "SELECT '{5,8,9,16}' AS s, JSON_OBJECT('a', 1) FROM t", "output": "y"})
F9 = '{"columns": ["a"]}\n\n{"sql": "SELECT 2", "output": "z"}'
F10 = '{"sql": "SELECT 3", "output": {"rows": [[1,'

CASES = [
    ("F1", FIXTURES / "h41-S4_qV4-36_gensql_response.txt", "MUST_FIX", "dict_with_sql"),
    ("F2", FIXTURES / "h43-S4_qV4-36_gensql_response.txt", "MUST_FIX", "dict_with_sql"),
    ("F3", FIXTURES / "h42-H0_qV4-36_gensql_response.txt", "MUST_FIX", "dict_with_sql"),
    ("F4", FIXTURES / "h42-S4_qV4-36_gensql_response.txt", "MUST_HOLD", "sql_none"),
    ("F5", FIXTURES / "h42-S4_qV4-31_gensql_response.txt", "MUST_HOLD", "none"),
    ("F6", None, "MUST_HOLD", "dict_with_sql", F6),
    ("F7", None, "MUST_HOLD", "dict_with_sql", F7),
    ("F8", None, "MUST_FIX", "dict_with_sql_braces", F8),
    ("F9", None, "MUST_FIX", "dict_with_sql", F9),
    ("F10", None, "MUST_HOLD", "baseline_equal", F10),
]


def classify(parsed) -> str:
    if parsed is None:
        return "none"
    if isinstance(parsed, dict):
        return "dict_with_sql" if isinstance(parsed.get("sql"), str) and parsed.get("sql") else "dict_no_sql"
    return f"type:{type(parsed).__name__}"


def run_suite() -> list[dict]:
    import datus.utils.json_utils as ju

    rows = []
    for case in CASES:
        cid, path, req, expected = case[0], case[1], case[2], case[3]
        text = case[4] if len(case) > 4 else path.read_text(encoding="utf-8")
        baseline = classify(ju.llm_result2json(text, expected_type=dict))
        yield_patch = f"_unused_{cid}"
        import r5patch

        r5patch._INSTALLED = False
        r5patch.install()
        patched = classify(ju.llm_result2json(text, expected_type=dict))
        # restore for next baseline: re-import fresh module state is complex;
        # instead compute baselines first for all cases in a pre-pass.
        rows.append({"id": cid, "req": req, "expected": expected, "baseline": baseline, "patched": patched,
                     "source": path.name if path else "synthetic", "len": len(text)})
    return rows


def main() -> int:
    # Pre-pass: baselines without any patch installed.
    import datus.utils.json_utils as ju

    baselines = {}
    for case in CASES:
        cid, path = case[0], case[1]
        text = case[4] if len(case) > 4 else path.read_text(encoding="utf-8")
        baselines[cid] = classify(ju.llm_result2json(text, expected_type=dict))

    import r5patch

    info = r5patch.install()
    rows = []
    failed = []
    for case in CASES:
        cid, path, req, expected = case[0], case[1], case[2], case[3]
        text = case[4] if len(case) > 4 else path.read_text(encoding="utf-8")
        patched = classify(ju.llm_result2json(text, expected_type=dict))
        base = baselines[cid]
        row = {"id": cid, "req": req, "expected": expected, "baseline": base, "patched": patched,
               "source": path.name if path else "synthetic", "len": len(text)}
        if req == "MUST_FIX":
            ok = patched == "dict_with_sql" or (cid == "F8" and patched == "dict_with_sql")
            if cid == "F8":
                # additionally verify the sql string kept its braces
                p = ju.llm_result2json(text, expected_type=dict)
                ok = isinstance(p, dict) and "{5,8,9,16}" in p.get("sql", "")
        else:
            if expected == "none":
                ok = patched == "none"
            elif expected == "sql_none":
                ok = patched in ("none", "dict_no_sql")
            elif expected == "dict_with_sql":
                ok = patched == "dict_with_sql"
            elif expected == "baseline_equal":
                ok = patched == base
            else:
                ok = False
        row["pass"] = bool(ok)
        if not ok:
            failed.append(cid)
        rows.append(row)

    report = {"patch_install": info, "runtime": sys.executable, "cases": rows, "failed": failed}
    out = HERE.parent / "fixtures" / "fixture-battery-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for row in rows:
        print(f"{row['id']:<4} {row['req']:<9} expected={row['expected']:<20} baseline={row['baseline']:<16} patched={row['patched']:<16} pass={row['pass']}")
    print("FAILED:", failed if failed else "none")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
