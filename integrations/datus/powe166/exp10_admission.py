"""POWE-166 EXP-10 executable offline admission + failure typology.

Part of round-5 work item 3: EXP-10 gains an *executable* admission check and
a failure typology, so the card's applicability is decided by a runnable
predicate instead of prose only.

Two entry points, both zero-model and read-only:

  1. admission(sql, schema_lookup) -> decision
     Parses a SQL statement, finds aggregate expressions AVG(...)/SUM(...),
     classifies the operand column (via INFORMATION_SCHEMA when available,
     otherwise via expression heuristics: function results like
     TIMESTAMPDIFF/COUNT are integer-valued), and decides whether the
     EXP-10 pre-aggregation CAST rule applies:
       ADMIT  when AVG/SUM would produce fractional values from
              integer-valued operands (the precision-risk case),
       REJECT when only COUNT/row-IDs/text/scale>=6 decimals appear
              (over-CAST guard, per EXP-10's own domain text).

  2. typology(expected_rows, got_rows) -> failure class for incorrect runs
       precision_miss : numeric cells off by a small relative margin
                        (fails 5e-7 but within 1e-3 relative), everything
                        else equal — the EXP-10 target failure family
                        (H4 V4-24: naked AVG over integers).
       null_vs_zero   : cells where the run produced 0/0.0 but the oracle
                        says NULL (or vice versa), everything else equal —
                        the empty-set COALESCE family (H4 V4-27).
       semantic       : row-shape/selection mismatch — not an EXP-10
                        precision matter.

Usage:
  python exp10_admission.py admit --sql-file q.sql [--schema host...]
  python exp10_admission.py typify --oracle o.json --got g.json
  python exp10_admission.py selftest   # saved H4 SQLs + saved H4 diffs
"""
from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

_AGG = re.compile(r"\b(AVG|SUM)\s*\(", re.IGNORECASE)
_COUNT = re.compile(r"\bCOUNT\s*\(", re.IGNORECASE)
_INTEGER_FNS = ("timestampdiff", "count", "row_number", "datediff", "unix_timestamp", "floor", "ceil")
_INT_TYPE = re.compile(r"^(tinyint|smallint|mediumint|int|integer|bigint|bit)", re.IGNORECASE)
_HIGH_SCALE = re.compile(r"decimal\s*\(\s*\d+\s*,\s*([6-9]|[1-9]\d)\s*\)", re.IGNORECASE)
_CAST = re.compile(r"\bCAST\s*\(", re.IGNORECASE)
_STRINGISH = re.compile(r"^(char|varchar|text|enum|set|date|datetime|timestamp|json)", re.IGNORECASE)


def _strip_string_literals(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def find_aggregates(sql: str) -> list[dict]:
    """Locate AVG/SUM call sites with their inner expression text."""
    cleaned = _strip_string_literals(sql)
    out = []
    for m in _AGG.finditer(cleaned):
        depth, i = 1, m.end()
        while i < len(cleaned) and depth:
            if cleaned[i] == "(":
                depth += 1
            elif cleaned[i] == ")":
                depth -= 1
            i += 1
        inner = cleaned[m.end():i - 1] if depth == 0 else cleaned[m.end():i]
        out.append({"fn": m.group(1).upper(), "expr": inner.strip(), "span_end": i})
    return out


def classify_operand(expr: str, schema_types: dict[str, str] | None = None) -> str:
    """integer | fractional | string | already_cast | unknown"""
    e = expr.strip().lower()
    if _CAST.search(expr):
        return "already_cast"
    if schema_types:
        # match against column references inside the expression
        for col, typ in schema_types.items():
            if re.search(rf"\b{re.escape(col.lower())}\b", e):
                t = typ.lower()
                if _INT_TYPE.match(t):
                    return "integer"
                if _HIGH_SCALE.search(t):
                    return "fractional"
                if _STRINGISH.match(t):
                    return "string"
                if t.startswith("decimal") or t.startswith("numeric") or t.startswith("float") or t.startswith("double"):
                    return "fractional"
    if any(fn in e for fn in _INTEGER_FNS):
        return "integer"  # integer-valued function result
    if e.isdigit() or re.fullmatch(r"-?\d+", e):
        return "integer"
    if re.search(r"/\s*\d", e) or "amount" in e or "price" in e or "value" in e:
        return "fractional"  # division or currency-ish heuristic
    return "unknown"


def admission(sql: str, schema_types: dict[str, str] | None = None) -> dict:
    aggs = find_aggregates(sql)
    cleaned = _strip_string_literals(sql)
    reasons, admits = [], []
    for a in aggs:
        kind = classify_operand(a["expr"], schema_types)
        if a["fn"] == "AVG":
            if kind == "already_cast":
                reasons.append(f"{a['fn']}({a['expr'][:40]}): operand already CAST -> no action")
            elif kind == "string":
                reasons.append(f"{a['fn']}({a['expr'][:40]}): string operand -> REJECT (no numeric cast)")
            else:
                admits.append(f"{a['fn']}({a['expr'][:40]}): AVG over {kind} operand -> CAST to DECIMAL(60,18) before aggregating")
        else:  # SUM
            if kind == "integer":
                # SUM of integers is exact; EXP-10 admits only when a later division/mean is applied
                reasons.append(f"SUM({a['expr'][:40]}): integer sum is exact -> no standalone CAST (admit only if later divided)")
            elif kind == "already_cast":
                reasons.append(f"SUM({a['expr'][:40]}): already CAST -> no action")
            else:
                admits.append(f"SUM({a['expr'][:40]}): {kind} operand -> keep exact, CAST if scale < 6")
    if not aggs:
        reasons.append("no AVG/SUM aggregate present -> EXP-10 not applicable")
    # Counterexample-driven guard (H4 V4-27): COALESCE around an aggregate turns
    # empty-group NULL into 0 — flag it so the NULL-preserving form is chosen.
    guards = []
    coalesce_agg = re.compile(r"\bCOALESCE\s*\([^)]*?\b(?:AVG|SUM)\s*\(", re.IGNORECASE)
    if coalesce_agg.search(cleaned):
        guards.append("COALESCE wraps an aggregate: empty-group NULL would become 0 "
                      "(H4 V4-27 counterexample) -> prefer NULL-preserving form or "
                      "COALESCE only at the outermost display layer")
    else:
        # CTE-alias form: agg CTE defines <alias> = AVG/SUM(...); outer query
        # wraps that alias in COALESCE (the actual V4-27 shape).
        agg_aliases = set()
        for m in re.finditer(r"\b(?:AVG|SUM)\s*\(", cleaned, re.IGNORECASE):
            depth, i = 1, m.end()
            while i < len(cleaned) and depth:
                if cleaned[i] == "(":
                    depth += 1
                elif cleaned[i] == ")":
                    depth -= 1
                i += 1
            tail = cleaned[i:i + 60]
            am = re.match(r"\s*(?:AS\s+)?([A-Za-z_]\w*)", tail, re.IGNORECASE)
            if am:
                agg_aliases.add(am.group(1).lower())
        if agg_aliases:
            for alias in sorted(agg_aliases):
                if re.search(rf"\bCOALESCE\s*\([^()]*(?:ROUND\s*\()?\s*[\w.]*\b{re.escape(alias)}\b",
                             cleaned, re.IGNORECASE):
                    guards.append(
                        f"COALESCE wraps aggregate-derived column '{alias}' (empty-group NULL -> 0, "
                        "H4 V4-27 counterexample) -> keep NULL for unmatched groups")
    # over-CAST guard: CAST on COUNT/ID/text contexts
    count_cast = re.compile(r"\bCAST\s*\(\s*(?:COUNT\s*\(.{0,40}?\)|[`.\w]*\bid\b|[`.\w]*_id\b)", re.IGNORECASE)
    if count_cast.search(cleaned):
        guards.append("CAST detected on COUNT/ID context -> over-CAST guard: integer-exact values need no precision cast")
    decision = "ADMIT" if admits else "REJECT"
    return {"decision": decision, "actions": admits, "non_actions": reasons,
            "guards": guards, "aggregates_found": len(aggs)}


def _cells(rows: list[list]) -> list:
    out = []
    for r_i, row in enumerate(rows):
        for c_i, v in enumerate(row):
            out.append((r_i, c_i, v))
    return out


def _num(v):
    """Unwrap reconcile Decimal dicts and parse numerics; None if not numeric."""
    if isinstance(v, dict) and "decimal" in v:
        v = v["decimal"]
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _norm_cell(v):
    """Canonical form for row matching: numeric equality across str/int/Decimal."""
    n = _num(v)
    if n is not None:
        return ("n", n)
    if isinstance(v, dict) and "decimal" in v:
        v = v["decimal"]
    if v is None:
        return ("null",)
    return ("s", str(v).strip())


def _align_rows(expected_rows, got_rows):
    """Multiset row alignment (scoring comparator is unordered). Returns list of
    (expected_row, got_row) pairs; leftover rows count as shape mismatches."""
    from collections import Counter

    got_pool = list(got_rows)
    pairs = []
    # pass 1: exact normalized matches
    for er in expected_rows:
        ek = tuple(_norm_cell(v) for v in er)
        for i, gr in enumerate(got_pool):
            if tuple(_norm_cell(v) for v in gr) == ek:
                pairs.append((er, gr))
                got_pool.pop(i)
                break
    # pass 2: best-effort pairing by differing-cell count (same width)
    for er in expected_rows:
        if any(er is p[0] for p in pairs):
            continue
        best, best_score = None, None
        for gr in got_pool:
            if len(gr) != len(er):
                continue
            score = sum(1 for a, b in zip(er, gr) if _norm_cell(a) != _norm_cell(b))
            if best_score is None or score < best_score:
                best, best_score = gr, score
        if best is not None:
            pairs.append((er, best))
            got_pool.remove(best)
    return pairs, len(got_pool)


def typology(expected_rows: list[list], got_rows: list[list]) -> dict:
    """Classify one incorrect run (multiset-aligned, position-free)."""
    if len(expected_rows) != len(got_rows) or (
        expected_rows and got_rows and len(expected_rows[0]) != len(got_rows[0])
    ):
        return {"class": "semantic", "detail": "row/column shape mismatch"}
    pairs, leftovers = _align_rows(expected_rows, got_rows)
    prec_cells, null_cells, other = 0, 0, 0
    zero_null, other_null = 0, 0
    examples = []
    for er, gr in pairs:
        for c, (ev, gv) in enumerate(zip(er, gr)):
            en, gn = _num(ev), _num(gv)
            if en is not None and gn is not None:
                if en == gn:
                    continue
                denom = abs(en) if en != 0 else Decimal(1)
                rel = abs(en - gn) / denom
                if rel <= Decimal("1e-3"):
                    prec_cells += 1
                    if len(examples) < 4:
                        examples.append({"cell": [c], "expected": str(ev), "got": str(gv), "rel": f"{rel:.2e}"})
                else:
                    other += 1
            elif (ev is None) != (gv is None):
                null_cells += 1
                zero = (_num(gv) == 0) if ev is None else (_num(ev) == 0)
                zero_null += int(zero)
                other_null += int(not zero)
                if len(examples) < 4:
                    examples.append({"cell": [c], "expected": ev, "got": gv})
            elif _norm_cell(ev) == _norm_cell(gv):
                continue
            else:
                other += 1
    detail = {"precision_cells": prec_cells, "null_cells": null_cells,
              "zero_vs_null_cells": zero_null, "other_null_cells": other_null,
              "other_cells": other, "unmatched_got_rows": leftovers}
    if prec_cells and not null_cells and not other:
        return {"class": "precision_miss", "cells": prec_cells, "examples": examples}
    if null_cells and not prec_cells and not other and not other_null:
        return {"class": "null_vs_zero" if zero_null else "null_semantics", "cells": null_cells, "examples": examples}
    return {"class": "semantic", "detail": detail, "examples": examples}



def bound_final_rows(result: dict):
    """Bind final SQL to its executed span (scorer semantics) and return (sql, rows)."""
    outp = result.get("output") or {}
    want = (outp.get("sql_query_final") or "").strip()
    if not want:
        return None, None
    norm = lambda s: s.strip().rstrip(";").strip().replace("%%", "%")
    match = [s for s in (result.get("sql_results") or []) if s.get("sql") and norm(s["sql"]) == norm(want)]
    if not match:
        return want, None
    return want, match[-1].get("rows")


def _h4_selftest() -> dict:
    """Run admission+typology on the saved H4 evidence cases."""
    here = Path(__file__).resolve().parent
    p161 = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861")
    import glob
    h4root = glob.glob(str(p161 / "powe-161-*" / "workdir" / "h4eval" / "evidence"))[0]
    results = {}

    # V4-24 S4 r3: naked AVG over TIMESTAMPDIFF -> ADMIT (precision_miss family)
    import json as _json
    def final_sql(batch, q):
        rj = Path(h4root) / batch / q / "result.json"
        r = _json.loads(rj.read_text())
        return ((r.get("output") or {}).get("sql_query_final") or "")

    sql_v424 = final_sql("h43-S4", "qV4-24")
    results["V4-24_admission"] = admission(sql_v424) if sql_v424 else {"error": "no final sql"}
    sql_v427 = final_sql("h41-S4", "qV4-27")
    results["V4-27_admission"] = admission(sql_v427) if sql_v427 else {"error": "no final sql"}

    # typology on the two known incorrect runs
    oracle_dir = glob.glob(str(p161 / "powe-161-*" / "workdir" / "h4eval" / "h4-package" / "bundle" / "oracle"))[0]
    for qid, batch, expect in (("V4-24", "h43-S4", "precision_miss"), ("V4-27", "h41-S4", "null_vs_zero")):
        oracle = _json.loads((Path(oracle_dir) / f"{qid}.json").read_text())
        rj = _json.loads((Path(h4root) / batch / f"q{qid}" / "result.json").read_text())
        got = None
        outp = rj.get("output") or {}
        # rows come from the final executed span bound by the scorer; emulate: last sql_result matching final
        finals = [s for s in (rj.get("sql_results") or []) if s.get("sql")]
        if finals and (outp.get("sql_query_final") or "").strip():
            norm = lambda s: s.strip().rstrip(";").strip().replace("%%", "%")
            want = norm(outp["sql_query_final"])
            match = [s for s in finals if norm(s["sql"]) == want]
            if match:
                got = match[-1].get("rows")
        if got is None:
            results[f"{qid}_typology"] = {"error": "cannot bind final rows"}
            continue
        t = typology(oracle["rows"], got)
        t["expected_class"] = expect
        t["match"] = t["class"] == expect
        results[f"{qid}_typology"] = t
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("admit")
    p1.add_argument("--sql-file")
    p1.add_argument("--sql")
    p2 = sub.add_parser("typify")
    p2.add_argument("--oracle")
    p2.add_argument("--got")
    sub.add_parser("selftest")
    args = parser.parse_args()
    if args.cmd == "admit":
        sql = args.sql or Path(args.sql_file).read_text(encoding="utf-8")
        print(json.dumps(admission(sql), ensure_ascii=False, indent=1))
    elif args.cmd == "typify":
        o = json.loads(Path(args.oracle).read_text())
        g = json.loads(Path(args.got).read_text())
        rows = g if isinstance(g, list) else g.get("rows")
        print(json.dumps(typology(o if isinstance(o, list) else o["rows"], rows), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(_h4_selftest(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
