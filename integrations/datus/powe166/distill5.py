"""POWE-166 counterexample-driven re-refinement: EXP-10v2 + round-5 arm commons.

Program-driven (no hand-invented rule text; template filled from evidence):

  corpus (all already-exposed H4 diagnostic material, no new blind data)
    - H4 h43-S4/qV4-24 (naked AVG over TIMESTAMPDIFF -> 8 precision_miss cells)
    - H4 h41-S4/qV4-27 (COALESCE(ROUND(mean,6),0) -> 21 null_vs_zero cells)
    - executable admission + failure typology (exp10_admission.py, this round)
    - EXP-10 v1 card text + lineage (POWE-158 distill.py, s4-manifest.json)

  induction
    - v1 applicability prose -> typed ADMIT/REJECT predicate (same conditions
      the executable admission enforces offline)
    - counterexample 1 (V4-24 r3): precision rule applied at fallback only ->
      v2 requires the inner CAST at FIRST generation for integer-valued
      operand AVG/SUM
    - counterexample 2 (V4-27 r1): aggregate output wrapped in COALESCE(...,0)
      -> v2 forbids empty-group NULL-to-0 substitution; oracle expects NULL
    - over-CAST counter-guards carried over from the R4 CE suite unchanged

  render (byte-disciplined)
    a00.txt  = s3v2.txt                                   (no EXP-10/11)
    a10.txt  = s3v2.txt + EXP-10 (v1)
    a01.txt  = s3v2.txt + EXP-11
    a11.txt  = s3v2.txt + EXP-10 + EXP-11 == s4.txt bytes (asserted)
    a11v2.txt= s3v2.txt + EXP-10v2 + EXP-11
    manifest freeze/r5-distill-manifest.json records every source hash.
"""
from __future__ import annotations

import glob
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import exp10_admission  # noqa: E402

R5 = HERE.parent
W = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861")
P152 = W / "powe-152-4bed59acb45b" / "workdir"
P158 = next(W.glob("powe-158-*")) / "workdir" / "r4"
H4 = next(W.glob("powe-161-*")) / "workdir" / "h4eval"
S3V2 = P152 / "r3" / "commons" / "s3v2.txt"
S4 = P158 / "artifacts" / "s4.txt"
ORACLE_DIR = H4 / "h4-package" / "bundle" / "oracle"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def bound_final_rows(result: dict):
    outp = result.get("output") or {}
    want = (outp.get("sql_query_final") or "").strip()
    if not want:
        return None, None
    norm = lambda s: s.strip().rstrip(";").strip().replace("%%", "%")
    match = [s for s in (result.get("sql_results") or []) if s.get("sql") and norm(s["sql"]) == norm(want)]
    if not match:
        return want, None
    return want, match[-1].get("rows")


def load_counterexample(batch: str, qid: str):
    qdir = H4 / "evidence" / batch / f"q{qid}"
    result = json.loads((qdir / "result.json").read_text(encoding="utf-8"))
    oracle = json.loads((ORACLE_DIR / f"{qid}.json").read_text(encoding="utf-8"))
    sql, rows = bound_final_rows(result)
    typ = exp10_admission.typology(oracle["rows"], rows) if rows is not None else {"class": "unbound"}
    adm = exp10_admission.admission(sql) if sql else {"decision": "unbound"}
    return {
        "qid": qid, "batch": batch,
        "trace_sha256": sha(qdir / "trace.jsonl"),
        "final_sql_head": (sql or "")[:160],
        "typology": typ, "admission": adm,
    }


def build_exp10v2(ce_v424: dict, ce_v427: dict, v1_text: str) -> str:
    prec_cells = ce_v424["typology"].get("cells", 0)
    null_cells = ce_v427["typology"].get("cells", 0)
    guards = ce_v427["admission"].get("guards", [])
    coalesce_aliases = sorted(
        {g.split("'")[1] for g in guards if "aggregate-derived column" in g}
    )
    adm_v424 = ce_v424["admission"]["decision"]
    return f"""## EXP-10-pre-aggregation-precision-v2 — typed admission + first-generation cast + NULL-preserving aggregation
- Applicability (typed, machine-checkable by exp10_admission.py offline): ADMIT when AVG (or SUM/COUNT division) has an integer-valued operand — integer columns, TIMESTAMPDIFF/DATEDIFF/COUNT-derived values, CASE...END over them — or a decimal6-contract numeric mean; REJECT for COUNT(*)/IDs/text, operands already CAST, or DECIMAL scale>=6. Admission decision on the V4-24 failing SQL was {adm_v424} (AVG(TIMESTAMPDIFF(...)) integer operand).
- Scope: birdbench numeric outputs under the normative solving contract (G3 decimal6, abs tolerance 5e-7); engine 5.7.25-OceanBase-v4.3.5.5, div_precision_increment=4
- Required tools: execute_sql
- Procedure:
  1. Engine fact (zero-model verified): AVG over an integer argument returns DECIMAL with only 4 fractional digits; a terminal ROUND(...,6) applied after the bare AVG CANNOT recover the lost scale.
  2. Apply the inner CAST at FIRST SQL generation, not as a mismatch fallback: wrap the aggregate argument as AVG(CAST(<arg> AS DECIMAL(60,18))), or equivalently CAST(AVG(<arg>) AS DECIMAL(30,10)); both forms are engine-verified to match the oracle. Counterexample (H4 V4-24 S4 r3, exposed diagnostic): the v1 card was consulted only at fallback and the naked AVG(TIMESTAMPDIFF(SECOND,...)) survived into the final SQL — {prec_cells} cells failed the 5e-7 oracle by lost scale alone (typology precision_miss).
  3. Keep existing NULL guards unchanged: CASE WHEN <match> THEN <arg> END stays around the argument; AVG over an empty or all-NULL set yields NULL with or without CAST (engine-verified).
  4. Do NOT round-ratio habits from EXP-07 onto integer AVG: ROUND may be applied only AFTER the cast, never as the only precision measure.
- Output contract: numeric mean columns emitted with >=6 exact fractional digits (decimal6 contract); aggregate outputs for unmatched/empty groups stay NULL
- NULL/empty (strengthened by counterexample): never substitute COALESCE(<aggregate>, 0) for empty groups. Counterexample (H4 V4-27 S4 r1, exposed diagnostic): COALESCE(ROUND(<agg-alias>,6),0) rewrote {null_cells} NULL mean cells to 0 (typology null_vs_zero) while every other cell matched; the normative oracle expects NULL. If a display alias must be substituted, do it only at the outermost presentation layer and never for group-level aggregates.
- Mismatch fallback: if an emitted mean differs from a verified high-precision value only in the 4th-6th fractional digits, suspect lost AVG scale first: recompute with the inner CAST form before changing any join/filter logic
- Inherits guards: G-EXACT-OUTPUT, G-NO-ROUND, G-DISTINCT-LIST, G-SCOPE-DENOM, G-SEMANTIC-BINDING, G-TIME-BOUNDARY, G-GROUP-KEY, G-NULL-EMPTY
- Counter-guards (recorded counterexamples):
  - applies to integer-argument aggregates and decimal6 means only — do NOT cast COUNT(*)/IDs/text (COUNT is invariant under CAST but casting adds nothing)
  - cast scale must be >=6; DECIMAL(n,<6) is forbidden (insufficient scale is as wrong as none)
  - non-temporal migration: the rule is family-independent (observed on AVG(Amount) probes), not a temporal-only habit
  - NULL-substitution counter-guard: COALESCE-to-0 around aggregate-derived columns ({', '.join(coalesce_aliases) if coalesce_aliases else 'agg aliases'}) is a typed failure (null_vs_zero), not a presentation choice
  - admission-typing counter-guard: if the offline admission predicate returns REJECT (COUNT/ID/text/scale>=6), adding CAST is the over-CAST failure mode, not extra safety
- Source: refined from v1 (POWE-158 distill.py lineage freeze/s4-manifest.json) by distill5.py from two exposed-H4 counterexample traces and the executable admission/typology tool; lineage in freeze/r5-distill-manifest.json
"""


def main() -> int:
    fx = R5 / "fixtures"
    v1_exp10 = (fx / "exp10-section.txt").read_text(encoding="utf-8")
    v1_exp11 = (fx / "exp11-section.txt").read_text(encoding="utf-8")
    s3 = S3V2.read_text(encoding="utf-8")

    ce_v424 = load_counterexample("h43-S4", "V4-24")
    ce_v427 = load_counterexample("h41-S4", "V4-27")
    assert ce_v424["typology"]["class"] == "precision_miss", ce_v424["typology"]
    assert ce_v427["typology"]["class"] == "null_vs_zero", ce_v427["typology"]

    exp10v2 = build_exp10v2(ce_v424, ce_v427, v1_exp10)

    art = R5 / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    arms = {
        "a00.txt": s3,
        "a10.txt": s3 + "\n" + v1_exp10,
        "a01.txt": s3 + "\n" + v1_exp11,
        "a11.txt": s3 + "\n" + v1_exp10 + v1_exp11,
        "a11v2.txt": s3 + "\n" + exp10v2 + v1_exp11,
    }
    for name, body in arms.items():
        (art / name).write_text(body, encoding="utf-8")

    s4_bytes = S4.read_bytes()
    assert (art / "a11.txt").read_bytes() == s4_bytes, "a11 must equal s4.txt byte-for-byte"

    manifest = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": {"s3v2_sha256": sha(S3V2), "s4_sha256": sha(S4)},
        "arms": {n: sha(art / n) for n in arms},
        "exp10_v1_sha256": hashlib.sha256(v1_exp10.encode()).hexdigest(),
        "exp11_sha256": hashlib.sha256(v1_exp11.encode()).hexdigest(),
        "exp10v2_sha256": hashlib.sha256(exp10v2.encode()).hexdigest(),
        "counterexamples": {"V4-24_h43-S4": ce_v424, "V4-27_h41-S4": ce_v427},
        "admission_tool_sha256": sha(HERE / "exp10_admission.py"),
        "note": "a11.txt asserted byte-identical to POWE-158 s4.txt (champion continuity); "
                "a11v2 differs only in the EXP-10 section; EXP-11 section byte-identical across arms that carry it.",
    }
    freeze = R5 / "freeze"
    freeze.mkdir(parents=True, exist_ok=True)
    (freeze / "r5-distill-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"arms": manifest["arms"], "assert_a11_eq_s4": True,
                      "ce_classes": [ce_v424["typology"]["class"], ce_v427["typology"]["class"]]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
