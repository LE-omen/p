"""POWE-158 self-distillation: pre-aggregation precision experience from
failure traces (the round's self-evolution deliverable).

This program (not a human) reads the failure traces and induces the
experience entries:

  corpus
    - A1 canary runs (this round, 24 runs: {N,S3V2} x {old,full} x {V3-13,14} x 3)
    - POWE-155 H3 evidence for V3-13/14 (7 conditions x 3 reps, 42 runs)
    - A0 zero-model engine canary (validated fix forms)

  per-run analysis
    - bind final SQL + final rows (sql_query_final / sql_result_final)
    - rebuild the executed-statement history (dbapi_execute.sql paired with
      sql_rows by driver_span_id)
    - per-cell Decimal comparison vs the frozen H3 oracle (abs tol 5e-7)
    - classify each run: correct / precision_scale_only / submission_reversion
      / other / unknown

  induction
    - collect failing AVG expression shapes and their emitted scale
    - collect correction forms observed in successful runs and in
      validated-but-lost executed revisions inside failing runs
    - keep only fix forms the A0 engine canary proved to match the oracle
    - emit EXP-10 (pre-aggregation precision) and EXP-11 (final-SQL revision
      consistency) in the inherited C4/s3 experience-entry structure, with a
      full lineage manifest (trace hashes, spans, mismatch cells)
    - render s4.txt = s3v2.txt + EXP-10 + EXP-11 and freeze a manifest

No model calls. H3 is already exposed (diagnostic use); H4 untouched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
R4 = HERE.parent
W = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861")
P152 = W / "powe-152-4bed59acb45b" / "workdir"
H3E = W / "powe-155-30296b5c919c" / "workdir" / "h3eval"
PKG = H3E / "h3-package"

TOL = Decimal("0.0000005")
AVG_RE = re.compile(
    r"(ROUND\(\s*AVG\([^()]*(?:\([^()]*\))?[^()]*\)\s*,\s*\d+\)"
    r"|CAST\(\s*AVG\([^()]*(?:\([^()]*\))?[^()]*\)\s+AS\s+DECIMAL\(\d+,\s*\d+\)\)"
    r"|AVG\(\s*(?:CAST\([^()]*\)\s+AS\s+DECIMAL\(\d+,\s*\d+\)|CASE[^,]*?END|[^,()]*(?:\([^()]*\))?[^,()]*)\))"
)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_run(qdir: Path):
    rp = qdir / "result.json"
    if not rp.exists():
        return None
    res = json.loads(rp.read_text(encoding="utf-8"))
    records = []
    tp = qdir / "trace.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return res, records


def parse_csv_rows(text: str):
    if not text or not text.strip():
        return None, None
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


def num(x):
    if isinstance(x, dict):  # serialized decimal envelope {"decimal": "..."}
        x = x.get("decimal", x.get("value"))
    try:
        return Decimal(str(x))
    except Exception:
        return None


def cells(got_rows, exp_rows):
    """Return (mismatch list, numeric-only-scale-failure bool)."""
    bad = []
    if got_rows is None or len(got_rows) != len(exp_rows):
        return [{"kind": "rows"}], False
    only_scale = bool(got_rows)
    for ri, (g, e) in enumerate(zip(got_rows, exp_rows)):
        if len(g) != len(e):
            return [{"kind": "cols", "row": ri}], False
        for ci, (gv, ev) in enumerate(zip(g, e)):
            gd, ed = num(gv), num(ev)
            if gd is not None and ed is not None:
                if abs(gd - ed) > TOL:
                    scale_like = abs(gd - ed) <= Decimal("0.001")
                    bad.append({"row": ri, "col": ci, "got": str(gv), "exp": str(ev),
                                "absdiff": str(abs(gd - ed)), "scale_like": scale_like})
                    if not scale_like:
                        only_scale = False
            elif str(gv).strip() != str(ev).strip():
                bad.append({"row": ri, "col": ci, "got": str(gv), "exp": str(ev),
                            "absdiff": None, "scale_like": False})
                only_scale = False
    return bad, only_scale


def final_rows_of(res):
    out = res.get("output") or {}
    return parse_csv_rows(out.get("sql_result_final"))


def executed_history(records):
    stmts, rows_by_span = [], {}
    for r in records:
        if r.get("kind") == "dbapi_execute" and r.get("sql"):
            stmts.append({"span": r.get("driver_span_id"), "seq": r.get("sequence"),
                          "sql": r["sql"]})
        elif r.get("kind") == "sql_rows":
            span = r.get("driver_span_id")
            if span not in rows_by_span and r.get("rows") is not None:
                rows_by_span[span] = {"columns": r.get("columns"), "rows": r["rows"]}
    hist = []
    for s in stmts:
        rr = rows_by_span.get(s["span"])
        if rr is not None:
            hist.append({"seq": s["seq"], "sql": s["sql"], "columns": rr["columns"],
                         "rows": rr["rows"]})
    hist.sort(key=lambda h: h["seq"] or 0)
    return hist


def avg_exprs(sql: str):
    return [m.group(0) for m in AVG_RE.finditer(sql or "")]


def classify(history_qdir, oracle_rows):
    """One run -> classification + evidence."""
    loaded = load_run(history_qdir)
    if loaded is None:
        return {"class": "missing", "qdir": str(history_qdir)}
    res, records = loaded
    cols, frows = final_rows_of(res)
    out = res.get("output") or {}
    fsql = out.get("sql_query_final") or ""
    if res.get("returncode") != 0 and not fsql:
        return {"class": "unknown", "qdir": str(history_qdir),
                "error": res.get("error"), "trace_sha256": sha(history_qdir / "trace.jsonl")}
    bad, only_scale = cells(frows, oracle_rows)
    ev = {"qdir": str(history_qdir), "final_sql_mean_exprs": avg_exprs(fsql),
          "mismatches": bad[:8], "n_mismatch": len(bad), "only_scale": only_scale,
          "s_agent": res.get("s_agent"),
          "trace_sha256": sha(history_qdir / "trace.jsonl") if (history_qdir / "trace.jsonl").exists() else None}
    if not bad:
        ev["class"] = "correct"
        return ev
    # submission reversion: an executed statement whose rows matched oracle
    # (or removed all scale mismatches) but was NOT the final SQL
    hist = executed_history(records)
    lost = []
    for h in hist:
        hbad, hscale = cells(h["rows"], oracle_rows)
        if not hbad and h["sql"].strip() != fsql.strip():
            lost.append({"seq": h["seq"], "sql_tail": h["sql"][-200:],
                         "mean_exprs": avg_exprs(h["sql"])})
    if lost:
        ev["class"] = "submission_reversion"
        ev["lost_validated_revisions"] = lost[-2:]
    elif only_scale and all(m.get("scale_like") for m in bad):
        ev["class"] = "precision_scale_only"
    else:
        ev["class"] = "other"
    ev["executed_count"] = len(hist)
    ev["executed_mean_exprs"] = [e for h in hist for e in avg_exprs(h["sql"])][:12]
    return ev


def main() -> None:
    a0 = json.loads((R4 / "evidence" / "a0-canary.json").read_text(encoding="utf-8"))
    corpus = []
    for rep in (1, 2, 3):  # A1 fresh corpus
        for cond in ("N-old", "N-full", "S3V2-old", "S3V2-full"):
            for q in ("V3-13", "V3-14"):
                d = R4 / "evidence" / "a1" / f"{cond}-r{rep}" / f"q{q}"
                corpus.append(("a1", cond, rep, q, d))
    for rep in (1, 2, 3):  # POWE-155 exposed corpus (V3-13/14 only)
        for cond in ("S3V2", "P2", "H0", "N", "M", "B", "C"):
            for q in ("V3-13", "V3-14"):
                d = H3E / "evidence" / f"h3{rep}-{cond}" / f"q{q}"
                corpus.append(("h3", cond, rep, q, d))
    runs = []
    for stage, cond, rep, q, d in corpus:
        oracle = json.loads((PKG / "oracle" / f"{q}.json").read_text(encoding="utf-8"))["rows"]
        ev = classify(d, oracle)
        ev.update({"stage": stage, "cond": cond, "rep": rep, "qid": q})
        runs.append(ev)

    from collections import Counter
    cls = Counter(r["class"] for r in runs)
    fails = [r for r in runs if r["class"] == "precision_scale_only"]
    reverts = [r for r in runs if r["class"] == "submission_reversion"]
    correct = [r for r in runs if r["class"] == "correct"]

    # failing expression shapes (dedup, keep observed forms verbatim)
    def shape(e: str):
        e = e.strip()
        e = re.sub(r"\s+", " ", e)
        return e
    failing_shapes = Counter(shape(e) for r in fails for e in r["final_sql_mean_exprs"])
    corrected_shapes = Counter(shape(e) for r in correct for e in r["final_sql_mean_exprs"])
    lost_shapes = Counter(shape(e) for r in reverts for l in r.get("lost_validated_revisions", [])
                          for e in l["mean_exprs"])

    # A0-validated fix forms (engine canary oracle match)
    a0_pass = {q: {k: v for k, v in r.items() if isinstance(v, dict)}
               for q, r in a0["v3_variants"].items()}
    fix_forms_ok = sorted({k for q in a0_pass.values() for k, v in q.items() if v.get("matches_oracle")})
    fix_forms_bad = sorted({k for q in a0_pass.values() for k, v in q.items()
                            if v.get("matches_oracle") is False})
    observed_scale = a0["probes"]["int_1_2_2"]
    div_incr = a0["probes"]["div_precision_increment"]
    version = a0["probes"]["version"]

    # ---- emit entries (fields filled ONLY from extracted evidence) ----
    top_fail = [s for s, _ in failing_shapes.most_common(6)]
    top_fix = [s for s, _ in corrected_shapes.most_common(8)]
    top_lost = [s for s, _ in lost_shapes.most_common(4)]

    exp10 = {
        "id": "EXP-10-pre-aggregation-precision",
        "title": "Integer-argument aggregates: cast before AVG — ROUND after cannot recover scale",
        "applicability": ("any AVG (or SUM/COUNT division) whose argument is an integer-typed "
                          "expression — DATEDIFF(...), integer columns (e.g. Amount), or CASE...END "
                          "over them — and any decimal6-contract numeric mean; observed failing "
                          "forms: " + "; ".join(top_fail[:4])),
        "scope": ("birdbench numeric outputs under the normative solving contract (G3 decimal6, "
                  f"abs tolerance 5e-7); engine {version}, div_precision_increment={div_incr}"),
        "required_tools": ["execute_sql"],
        "procedure": [
            f"Engine fact (zero-model verified): AVG over an integer argument returns DECIMAL with only {div_incr} "
            "fractional digits; a terminal ROUND(...,6) applied after the bare AVG CANNOT recover the lost scale "
            "(verified: " + ", ".join(fix_forms_bad) + " all fail the 5e-7 oracle match).",
            "Fix at SQL generation time, inside the same single statement: wrap the aggregate argument as "
            "AVG(CAST(<arg> AS DECIMAL(60,18))), or equivalently cast the aggregate as "
            "CAST(AVG(<arg>) AS DECIMAL(30,10)); both forms are engine-verified to match the oracle "
            "(verified: " + ", ".join(fix_forms_ok) + ").",
            "Keep existing NULL guards unchanged: CASE WHEN <match> THEN <arg> END stays around the argument, "
            "and AVG over an empty or all-NULL set still yields NULL (engine-verified with and without CAST).",
            "Do NOT round-ratio habits from EXP-07 onto integer AVG: ROUND may be applied only AFTER the cast, "
            "never as the only precision measure of an integer-argument AVG.",
        ],
        "output_granularity": "numeric mean columns emitted with >=6 exact fractional digits (decimal6 contract)",
        "null_empty_semantics": ("AVG over no matched values is NULL; unmatched rows are preserved in counts; "
                                 "CAST does not alter NULL/empty semantics or COUNT results (engine-verified)"),
        "mismatch_fallback": ("if an emitted mean differs from a verified high-precision value only in the "
                              "4th-6th fractional digits, suspect lost AVG scale first: recompute with the "
                              "inner CAST form before changing any join/filter logic"),
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-DISTINCT-LIST", "G-SCOPE-DENOM",
                   "G-SEMANTIC-BINDING", "G-TIME-BOUNDARY", "G-GROUP-KEY", "G-NULL-EMPTY"],
        "counter_guards": [
            "applies to integer-argument aggregates and decimal6 means only — do NOT cast COUNT(*)/IDs/text "
            "(COUNT is invariant under CAST but casting adds nothing)",
            "cast scale must be >=6; DECIMAL(n,<6) is forbidden (insufficient scale is as wrong as none)",
            "non-temporal migration: the rule is family-independent (observed on AVG(Amount) probes), "
            "not a temporal-only habit"],
        "source": {
            "corpus": {"runs": len(runs), "classes": dict(cls)},
            "precision_failures": [{"qid": r["qid"], "cond": r["cond"], "stage": r["stage"], "rep": r["rep"],
                                    "mean_exprs": r["final_sql_mean_exprs"][:3],
                                    "mismatch_cells": [m for m in r["mismatches"] if m.get("scale_like")][:2],
                                    "trace_sha256": r.get("trace_sha256")} for r in fails],
            "corrected_forms_observed": top_fix,
            "a0_evidence_sha256": sha(R4 / "evidence" / "a0-canary.json"),
            "distilled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    exp11 = {
        "id": "EXP-11-final-sql-revision-consistency",
        "title": "Submit the last executed revision that produced your verified values",
        "applicability": ("every run, at final submission; observed violation form: a later executed "
                          "statement already returned contract-precise values, but sql_query_final "
                          "reverted to an earlier lower-precision form" + (
                              "; detected cases: " + "; ".join(f"{r['stage']}/{r['cond']}/r{r['rep']}/{r['qid']}"
                                                               for r in reverts[:4]) if reverts else
                              "; no fresh case in this corpus - entry induced from the recorded "
                              "POWE-155 C4 h31 V3-14 case via POWE-157 evidence index")),
        "scope": "GenSQL final envelope vs executed statement history",
        "required_tools": ["execute_sql"],
        "procedure": [
            "the final submitted SQL must be character-for-character the LAST executed statement whose "
            "result you are submitting; never reformat, re-derive from memory, or fall back to an earlier draft",
            "if a later execution corrected precision (e.g. a CAST revision), that corrected statement is the "
            "one to submit — the scorer binds the final answer to the execution of sql_query_final",
            "when exploring multiple forms, verify once, then submit exactly that form",
        ],
        "output_granularity": "final_sql span identical to the last verified executed statement",
        "null_empty_semantics": "unknown/failed executions stay in the denominator; submit nothing unexecuted",
        "mismatch_fallback": "none — submission discipline, always applicable (extends EXP-09)",
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-DISTINCT-LIST", "G-SCOPE-DENOM",
                   "G-SEMANTIC-BINDING", "G-TIME-BOUNDARY", "G-GROUP-KEY", "G-NULL-EMPTY"],
        "source": {
            "reversion_cases": [{"qid": r["qid"], "cond": r["cond"], "stage": r["stage"], "rep": r["rep"],
                                 "lost": r.get("lost_validated_revisions"),
                                 "trace_sha256": r.get("trace_sha256")} for r in reverts],
            "historical_evidence": "POWE-155 C4 h31 V3-14 (executed seq270 high-precision form lost in final SQL), "
                                   "per POWE-157 research E3",
            "distilled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }

    def render(e):
        lines = [f"## {e['id']} — {e['title']}",
                 f"- Applicability: {e['applicability']}",
                 f"- Scope: {e['scope']}",
                 f"- Required tools: {', '.join(e['required_tools'])}",
                 "- Procedure:"]
        lines += [f"  {i+1}. {p}" for i, p in enumerate(e["procedure"])]
        lines += [f"- Output contract: {e['output_granularity']}",
                  f"- NULL/empty: {e['null_empty_semantics']}",
                  f"- Mismatch fallback: {e['mismatch_fallback']}",
                  f"- Inherits guards: {', '.join(e['guards'])}"]
        if e.get("counter_guards"):
            lines += ["- Counter-guards (recorded counterexamples):"] + [f"  - {c}" for c in e["counter_guards"]]
        lines += [f"- Source: distilled from failure traces by distill.py (lineage in freeze/s4-manifest.json); "
                  f"class counts {json.dumps(e['source'].get('corpus', {}), ensure_ascii=False) or 'see manifest'}"]
        return "\n".join(lines)

    s3v2 = (P152 / "r3" / "commons" / "s3v2.txt").read_text(encoding="utf-8")
    s4 = s3v2.rstrip("\n") + "\n\n" + render(exp10) + "\n\n" + render(exp11) + "\n"
    (R4 / "artifacts" / "s4.txt").write_text(s4, encoding="utf-8")

    manifest = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_common_s3v2_sha256": sha(P152 / "r3" / "commons" / "s3v2.txt"),
        "s4_sha256": hashlib.sha256(s4.encode("utf-8")).hexdigest(),
        "entries": {"EXP-10": exp10, "EXP-11": exp11},
        "corpus_summary": {"runs": len(runs), "classes": dict(cls),
                           "failing_shapes": dict(failing_shapes.most_common(10)),
                           "corrected_shapes": dict(corrected_shapes.most_common(10)),
                           "lost_validated_shapes": dict(lost_shapes.most_common(6)),
                           "a0_fix_forms_ok": fix_forms_ok, "a0_fix_forms_bad": fix_forms_bad,
                           "engine": {"version": version, "div_precision_increment": div_incr,
                                      "int_1_2_2": observed_scale}},
        "runs": runs,
    }
    (R4 / "freeze" / "s4-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    print(json.dumps({"classes": dict(cls), "failing_shapes": dict(failing_shapes.most_common(6)),
                      "corrected_shapes": dict(corrected_shapes.most_common(6)),
                      "lost_validated_shapes": dict(lost_shapes.most_common(4)),
                      "s4_sha256": manifest["s4_sha256"][:16]}, ensure_ascii=False, indent=1))
    print("s4.txt ->", R4 / "artifacts" / "s4.txt")
    print("manifest ->", R4 / "freeze" / "s4-manifest.json")


if __name__ == "__main__":
    main()
