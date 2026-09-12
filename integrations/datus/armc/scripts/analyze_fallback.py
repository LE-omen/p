"""POWE-138 ARM-C: fallback accounting for R runs.

Classifies each final SQL as a descriptor-template hit (which family) or a
native-path fallback, via mechanical regexes derived from the template
skeletons; then aggregates fallback rate and cost (S_agent, model tokens)
per class. No model judgment.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

FAMILY_RE = [
    ("T01-dim-filter-count", re.compile(r"^SELECT\s+COUNT\([^)]+\)\s+FROM\s+(gasstations|customers|products)\s+WHERE\s+.+=.+$", re.I | re.S)),
    ("T02-cust-ym-consumption-extremum", re.compile(r"FROM\s+customers.*INNER\s+JOIN\s+yearmonth.*GROUP\s+BY.*ORDER\s+BY\s+SUM\([^)]*Consumption\)\s+(ASC|DESC)\s+LIMIT\s+1", re.I | re.S)),
    ("T03-segment-agg-extremum", re.compile(r"^SELECT\s+\S+\.Segment\s+FROM\s+customers.*INNER\s+JOIN\s+yearmonth.*GROUP\s+BY\s+\S+\.Segment\s+ORDER\s+BY\s+SUM", re.I | re.S)),
    ("T04-peak-month", re.compile(r"SELECT\s+SUBSTR\(\S*[.]?\w*,\s*5,\s*2\).*GROUP\s+BY\s+SUBSTR\(.*\)\s+ORDER\s+BY\s+SUM.*DESC\s+LIMIT\s+1", re.I | re.S)),
    ("T05-conditional-agg", re.compile(r"SELECT\s+(CAST\()?\s*SUM\s*\(\s*IF\s*\(|SUM\s*\(\s*\w+\s*=\s*'|SUM\(IF\(SUBSTR", re.I)),
    ("T06-distinct-list-join", re.compile(r"^SELECT\s+DISTINCT\s+\S+\s+FROM\s+transactions_1k.*INNER\s+JOIN", re.I | re.S)),
    ("T07-txn-filtered-aggregate", re.compile(r"^SELECT\s+(COUNT|AVG)\s*\([^)]*\)\s+FROM\s+transactions_1k\s+AS\s+\S+\s+INNER\s+JOIN", re.I | re.S)),
    ("T08-point-lookup", re.compile(r"WHERE\s+\S*[.]?Date\s*=\s*'\d{4}-\d{2}-\d{2}'\s+AND\s+\S*[.]?Time\s*=\s*'\d{2}:\d{2}:\d{2}'|GROUP\s+BY\s+CustomerID\s+ORDER\s+BY\s+SUM\(\S*[.]?Price\)\s+DESC\s+LIMIT\s+1|Consumption\s*=\s*\d", re.I)),
]


def classify(sql: str | None) -> str:
    if not sql:
        return "no_final_sql"
    norm = re.sub(r"\s+", " ", sql.strip().rstrip(";"))
    for fam, rx in FAMILY_RE:
        if fam in ("T05-conditional-agg", "T08-point-lookup"):
            if rx.search(norm):
                return fam
        elif rx.search(norm):
            return fam
    return "native_fallback"


def main() -> None:
    proof = json.loads((HERE / "candidates" / "freeze-proof.json").read_text(encoding="utf-8"))
    cid = proof["champion"]
    rows = []
    for i in (1, 2, 3):
        root = HERE / "evidence" / f"r{i}-{cid}"
        for qdir in sorted(root.iterdir()):
            rp = qdir / "result.json"
            if not rp.exists():
                continue
            r = json.loads(rp.read_text(encoding="utf-8"))
            output = r.get("output") or {}
            sql = output.get("sql_query_final") or (r.get("answer") or {}).get("sql") if isinstance(r.get("answer"), dict) else None
            if not sql and isinstance(output, dict):
                sql = output.get("sql_query_final")
            led = r.get("step_ledger") or {}
            rows.append({"run": f"r{i}", "qid": qdir.name[1:], "family": classify(sql),
                         "s_agent": r.get("s_agent"),
                         "correct": None,  # filled from r3-report
                         "model_requests": led.get("model_requests"),
                         "total_tokens": led.get("total_tokens")})
    try:
        r3 = json.loads((HERE / "candidates" / "r3-report.json").read_text(encoding="utf-8"))
        for row in rows:
            per = r3["per_question"].get(row["qid"], {}).get(row["run"], {})
            row["correct"] = per.get("correct")
            row["in_budget"] = per.get("in_budget")
    except FileNotFoundError:
        pass
    hits = [r for r in rows if r["family"].startswith("T")]
    fb = [r for r in rows if r["family"] == "native_fallback"]
    other = [r for r in rows if r["family"] not in ("native_fallback",) and not r["family"].startswith("T")]

    def agg(rs):
        s = [r["s_agent"] for r in rs if r["s_agent"] is not None]
        t = [r["total_tokens"] for r in rs if r.get("total_tokens")]
        c = [r for r in rs if r.get("correct") is True]
        j = [r for r in rs if r.get("in_budget")]
        return {"n": len(rs), "mean_s_agent": round(sum(s) / len(s), 2) if s else None,
                "mean_tokens": round(sum(t) / len(t)) if t else None,
                "correct": len(c), "j": len(j)}

    out = {"rows": rows,
           "template_hits": agg(hits), "native_fallback": agg(fb), "unclassified": agg(other),
           "fallback_rate": round(len(fb) / len(rows), 3) if rows else None,
           "by_family": {}}
    for r in rows:
        out["by_family"][r["family"]] = out["by_family"].get(r["family"], 0) + 1
    (HERE / "candidates" / "fallback-analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"total={len(rows)} template_hits={len(hits)} native_fallback={len(fb)} other={len(other)} fallback_rate={out['fallback_rate']}")
    print("hits:", out["template_hits"])
    print("fallback:", out["native_fallback"])
    print("families:", out["by_family"])


if __name__ == "__main__":
    main()
