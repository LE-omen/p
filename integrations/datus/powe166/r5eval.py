"""POWE-166 round-5 driver: contract-verified EXP-10/11 2x2 ablation + v2 arm.

Design (frozen in freeze/prereg-r5.json BEFORE any model run):
  arms (single-factor common-text variation; skills/contract/parser/runner identical)
    A00   = s3v2 common                                   (no EXP-10, no EXP-11)
    A10   = s3v2 + EXP-10(v1)
    A01   = s3v2 + EXP-11
    A11   = s3v2 + EXP-10(v1) + EXP-11  == s4.txt bytes (champion continuity)
    A11v2 = s3v2 + EXP-10(v2) + EXP-11  (counterexample-refined candidate)
  questions (12, all already-exposed diagnostic material; no new blind data)
    precision family : V3-13, V3-14, V4-24, V4-25, V4-26   (EXP-10 target family)
    null-semantics    : V4-27                              (empty-group NULL vs 0 probe)
    state-lookup      : V4-31                              (zero-qty invalidation probe)
    strict-structure  : V4-32, V4-36, V4-38, V4-39         (interference probes + parse-fix probes)
    weighted-semantics: V4-45                              (card-interference watch, R4 flag)
  5 arms x 12 questions x 3 reps = 180 runs.

This round's deltas vs the H4 evaluation stack (all frozen here):
  - contract IS delivered (run_qa_r5.py payload propagation; per-run
    delivered_common_sha256 asserted against the expected with-contract digest)
  - parser A-fix installed for every arm (r5patch; fixture battery 10/10)
  - monitor reads the real fields (final binding via output.sql_query_final;
    executed spans via result.sql_results) and types every failure
    (exp10_admission.typology), replacing the H4 monitor's broken
    action.args.sql reads (null cast_in_final_sql / empty sql_spans)

Usage (scoring venv python; runs spawn the datus runtime venv):
  python r5eval.py prereg
  python r5eval.py run <arm> <rep>        # one batch, resumable + assembly retry
  python r5eval.py matrix [arms]          # rep-major interleave over all arms
  python r5eval.py score
  python r5eval.py monitor
  python r5eval.py cost
  python r5eval.py verdict
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
R5 = HERE.parent
W = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861")
P152 = W / "powe-152-4bed59acb45b" / "workdir"
P155 = W / "powe-155-30296b5c919c" / "workdir" / "h3eval"
H3PKG = P155 / "h3-package"
H4E = next(W.glob("powe-161-*")) / "workdir" / "h4eval"
V4PKG = H4E / "h4-package" / "bundle"

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(R5 / "powercontext" / "integrations" / "datus" / "src"))
sys.path.insert(0, str(R5 / "powercontext" / "src"))
import exp10_admission  # noqa: E402
from powercontext_datus.scoring import score_case  # noqa: E402

RUNTIME_PY = P152 / "datus-runtime" / ".venv" / "bin" / "python"
RUNNER = HERE / "run_qa_r5.py"
CONTRACT = next(W.glob("powe-158-*")) / "workdir" / "r4" / "contract" / "numeric-contract-v1.md"
ART = R5 / "artifacts"
P2_SKILLS_DIR = P152 / "artifacts" / "p2-additive"
P2_SKILLS = ["d2-patterns", "legacy-patterns", "negative-guards"]

DB_HOSTNAME = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
MAX_TURNS, TIMEOUT_S, CONCURRENCY = 8, 420, 3
CURRENT_DATE = "2026-09-13"
REPS = 3

ARMS = {
    "A00":   {"label": "s3v2 base (no EXP-10/11)", "common": ART / "a00.txt", "exp10": False, "exp11": False},
    "A10":   {"label": "s3v2 + EXP-10 v1", "common": ART / "a10.txt", "exp10": True, "exp11": False},
    "A01":   {"label": "s3v2 + EXP-11", "common": ART / "a01.txt", "exp10": False, "exp11": True},
    "A11":   {"label": "s3v2 + EXP-10 v1 + EXP-11 (== S4 common e4af9661)", "common": ART / "a11.txt", "exp10": True, "exp11": True},
    "A11v2": {"label": "s3v2 + EXP-10 v2 + EXP-11 (refined candidate)", "common": ART / "a11v2.txt", "exp10": "v2", "exp11": True},
}

FAMILIES = {
    "precision": ["V3-13", "V3-14", "V4-24", "V4-25", "V4-26"],
    "null_semantics": ["V4-27"],
    "state_lookup": ["V4-31"],
    "strict_structure": ["V4-32", "V4-36", "V4-38", "V4-39"],
    "weighted_semantics": ["V4-45"],
}
ALLQ_IDS = [q for fam in FAMILIES.values() for q in fam]
NON_PRECISION = [q for fam in ("null_semantics", "state_lookup", "strict_structure", "weighted_semantics") for q in FAMILIES[fam]]

V3Q = {q["id"]: q for q in json.loads((H3PKG / "questions.json").read_text(encoding="utf-8"))["questions"]
       if q["id"] in ("V3-13", "V3-14")}
V4Q = {q["id"]: q for q in json.loads((V4PKG / "questions.json").read_text(encoding="utf-8"))["questions"]
       if q["partition"] == "H4"}
ALLQ = {**V3Q, **{k: v for k, v in V4Q.items() if k in ALLQ_IDS}}
assert sorted(ALLQ) == sorted(ALLQ_IDS), (sorted(ALLQ), sorted(ALLQ_IDS))

ANSWER_PROTOCOL = (
    "Return the native GenSQL JSON envelope with sql and output. "
    'The entire output must be a JSON string encoding {"columns": [...], "rows": [[...]]}, '
    "containing the complete answer table. Preserve NULL, duplicate rows and column order. "
    "Do not include prose or unsupported claims in output."
)

BUDGET = {"request_attempts_cap": 900, "input_tokens_cap": 45_000_000, "output_tokens_cap": 3_500_000}

LINEAGE = R5 / "lineage.jsonl"


def log(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with LINEAGE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def expected_common_digest(arm: str) -> str:
    common = (ARMS[arm]["common"]).read_text(encoding="utf-8")
    body = common + "\n\n" + CONTRACT.read_text(encoding="utf-8") + "\n\n" + ANSWER_PROTOCOL
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def expected(qid: str):
    oracle = V3PKG_ORACLE / f"{qid}.json" if qid.startswith("V3-") else V4PKG / "oracle" / f"{qid}.json"
    o = json.loads(oracle.read_text(encoding="utf-8"))
    return o["rows"], bool(ALLQ[qid].get("contract", {}).get("ordered", False))


V3PKG_ORACLE = H3PKG / "oracle"


def resolve_db_ip() -> str:
    last = None
    for _ in range(6):
        try:
            return socket.gethostbyname(DB_HOSTNAME)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    raise SystemExit(f"cannot resolve {DB_HOSTNAME}: {last}")


def load_env() -> None:
    for line in Path("/home/rongfeng.frf/workspace/.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    if "DB_PASSWORD" not in os.environ:
        raise SystemExit("DB_PASSWORD missing from environment")


def make_plan(arm: str, r: int, ip: str) -> Path:
    cfg = ARMS[arm]
    out_root = R5 / "evidence" / f"r{r}-{arm}"
    qs = [{"qid": qid, "question": ALLQ[qid]["question"], "arm": arm, "task_id": qid,
           "skill_names": P2_SKILLS, "inject_skills": True}
          for qid in sorted(ALLQ_IDS)]
    plan = {"run_id": f"powe166-r5-{r}-{arm.lower()}", "out_root": str(out_root),
            "db": {"host": ip, "port": 3306, "username": "mock_data_readonly", "name": "birdbench"},
            "db_hostname_resolved": {"hostname": DB_HOSTNAME, "ip": ip},
            "model": MODEL, "common_path": str(cfg["common"]),
            "contract_path": str(CONTRACT),
            "skills_dir": str(P2_SKILLS_DIR), "skill_names": P2_SKILLS,
            "inject_skills": True,
            "max_turns": MAX_TURNS, "timeout_s": TIMEOUT_S, "current_date": CURRENT_DATE,
            "concurrency": CONCURRENCY, "condition_label": cfg["label"], "questions": qs}
    out = R5 / "plans" / f"powe166-r5-{r}-{arm.lower()}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def run_batch(arm: str, r: int, retries: int = 3) -> None:
    load_env()
    out_root = R5 / "evidence" / f"r{r}-{arm}"
    for attempt in range(1, retries + 1):
        plan_path = make_plan(arm, r, resolve_db_ip())
        t0 = time.time()
        proc = subprocess.run([str(RUNTIME_PY), str(RUNNER), "--batch", str(plan_path)],
                              cwd=str(R5 / "harness"), capture_output=True, text=True)
        assembly_failed = []
        for qid in sorted(ALLQ_IDS):
            rj = out_root / f"q{qid}" / "result.json"
            if rj.exists():
                res = json.loads(rj.read_text(encoding="utf-8"))
                if res.get("returncode") == 1 and res.get("steps") is None:
                    tj = rj.parent / "trace.jsonl"
                    injected = tj.exists() and "question_injected" in tj.read_text(encoding="utf-8")
                    if not injected:
                        assembly_failed.append(qid)
        log({"event": "exec", "arm": arm, "rep": r, "attempt": attempt, "rc": proc.returncode,
             "seconds": round(time.time() - t0, 1), "assembly_failed": assembly_failed})
        print(f"[r{r}-{arm}] attempt={attempt} rc={proc.returncode} "
              f"elapsed={time.time()-t0:.0f}s assembly_failed={assembly_failed}", flush=True)
        if proc.returncode != 0:
            print("STDERR tail:", (proc.stderr or "")[-600:], flush=True)
        if not assembly_failed:
            return
        for qid in assembly_failed:
            (out_root / f"q{qid}" / "result.json").unlink(missing_ok=True)
    print(f"WARNING: {arm} rep{r} still has assembly failures after {retries} attempts", flush=True)


def matrix(arms: list[str]) -> None:
    for r in (1, 2, 3):  # rep-major interleave
        for arm in arms:
            run_batch(arm, r)


# ---------------------------------------------------------------- scoring ---

def load_case(qdir: Path):
    result_path, trace_path = qdir / "result.json", qdir / "trace.jsonl"
    if not result_path.exists():
        return None, None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    records = []
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return result, records


def unknown_typology(result: dict, case: dict) -> str:
    if case.get("unknown_reason") is None:
        return ""
    reason = case.get("unknown_reason")
    err = (result.get("error") or "") + " " + " ".join(result.get("trace_issues") or [])
    if reason == "final_binding_failed":
        return "final_binding"
    if "TimeoutError" in err or "timeout" in reason.lower():
        return "timeout"
    if "MaxTurns" in err or "max turns" in err.lower():
        return "max_turns"
    if "No SQL context" in err or "200002" in err:
        return "handoff_no_sql_context"
    if reason == "run_failed":
        return "run_failed"
    return "other_unknown"


def family_of(qid: str) -> str:
    for fam, members in FAMILIES.items():
        if qid in members:
            return fam
    return "?"


def score_run(arm: str, r: int) -> list[dict]:
    out_root = R5 / "evidence" / f"r{r}-{arm}"
    want_digest = expected_common_digest(arm)
    cases = []
    for qid in sorted(ALLQ_IDS):
        result, records = load_case(out_root / f"q{qid}")
        rows, ordered = expected(qid)
        case = score_case(result, records, rows, ordered=ordered)
        case.update(qid=qid, family=family_of(qid), rep=r, arm=arm)
        # delivery verification (per run)
        if result is not None:
            got = result.get("delivered_common_sha256")
            case["contract_delivered"] = bool(result.get("contract_path_delivered")) and got == want_digest
            case["delivery_digest_match"] = (got == want_digest)
        else:
            case["contract_delivered"] = None
        # failure typing for incorrect runs (multiset-aligned, position-free)
        if case["correct"] is False:
            sql, got_rows = exp10_admission.bound_final_rows(result or {})
            if got_rows is not None:
                case["failure_type"] = exp10_admission.typology(rows, got_rows)["class"]
            else:
                case["failure_type"] = "unbound_incorrect"
        elif case["correct"] is None:
            case["failure_type"] = unknown_typology(result or {}, case)
        else:
            case["failure_type"] = ""
        cases.append(case)
    return cases


def t_ci(diffs: list):
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(diffs) / n
    if n >= 3:
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        crit = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365,
                9: 2.306, 10: 2.262, 11: 2.228, 12: 2.201, 15: 2.145, 18: 2.101,
                20: 2.093, 24: 2.069, 30: 2.045, 36: 2.028}.get(n, 1.96)
    else:
        var = sum((d - mean) ** 2 for d in diffs) / n
        crit = 1.96
    half = crit * math.sqrt(var / n) if var >= 0 else 0.0
    return mean, mean - half, mean + half


def paired(report: dict, arm_x: str, arm_y: str, qids: list[str]) -> dict:
    """Rep-matched paired correctness diffs (arm_x - arm_y) over given questions."""
    diffs, per_q = [], {}
    for qid in qids:
        qd = []
        for r in (1, 2, 3):
            x = report["cases"][(arm_x, r, qid)]
            y = report["cases"][(arm_y, r, qid)]
            xv = 1 if x["correct"] is True else 0
            yv = 1 if y["correct"] is True else 0
            qd.append(xv - yv)
        per_q[qid] = qd
        diffs.extend(qd)
    mean, lo, hi = t_ci(diffs)
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    return {"per_question_rep_diffs": per_q, "n_pairs": len(diffs),
            "mean": round(mean, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "wins": wins, "losses": losses, "ties": len(diffs) - wins - losses}


def score() -> dict:
    report = {"cases": {}, "arm_summary": {}, "family_summary": {}, "delivery": {},
              "paired": {}, "failure_types": {}}
    for arm in ARMS:
        if not (R5 / "evidence" / f"r1-{arm}").exists():
            continue
        all_cases = []
        for r in (1, 2, 3):
            for c in score_run(arm, r):
                report["cases"][(arm, r, c["qid"])] = c
                all_cases.append(c)
        known_s = [c["s_agent"] for c in all_cases if c.get("s_agent") is not None]
        correct = sum(1 for c in all_cases if c["correct"] is True)
        j2 = sum(1 for c in all_cases if c.get("in_budget") is True)
        j4 = sum(1 for c in all_cases if c["correct"] is True and c.get("s_agent") is not None and c["s_agent"] <= 4)
        report["arm_summary"][arm] = {
            "runs": len(all_cases), "correct": correct, "unknown": sum(1 for c in all_cases if c["correct"] is None),
            "j2": j2, "j4": j4,
            "mean_s_agent": round(sum(known_s) / len(known_s), 2) if known_s else None,
        }
        fam = {}
        for f, members in FAMILIES.items():
            fc = [c for c in all_cases if c["family"] == f]
            fam[f] = {"n": len(fc), "correct": sum(1 for c in fc if c["correct"] is True),
                      "unknown": sum(1 for c in fc if c["correct"] is None),
                      "j2": sum(1 for c in fc if c.get("in_budget") is True),
                      "failure_types": sorted({c.get("failure_type") for c in fc if c.get("failure_type")})}
        report["family_summary"][arm] = fam
        report["delivery"][arm] = {
            "contract_delivered_runs": sum(1 for c in all_cases if c.get("contract_delivered") is True),
            "delivery_violations": [f"r{c['rep']}/{c['qid']}" for c in all_cases if c.get("contract_delivered") is False],
        }
        ft = {}
        for c in all_cases:
            if c.get("failure_type"):
                ft[c["failure_type"]] = ft.get(c["failure_type"], 0) + 1
        report["failure_types"][arm] = ft

    # EXP-10 marginal effect (same EXP-11 background), EXP-11 marginal effect
    if all(a in report["arm_summary"] for a in ARMS):
        for fset_name, qids in (("precision", FAMILIES["precision"]),
                                ("non_precision", NON_PRECISION),
                                ("all12", ALLQ_IDS)):
            report["paired"][f"EXP10_effect_A10-A00_{fset_name}"] = paired(report, "A10", "A00", qids)
            report["paired"][f"EXP10_effect_A11-A01_{fset_name}"] = paired(report, "A11", "A01", qids)
            report["paired"][f"EXP11_effect_A01-A00_{fset_name}"] = paired(report, "A01", "A00", qids)
            report["paired"][f"EXP11_effect_A11-A10_{fset_name}"] = paired(report, "A11", "A10", qids)
            report["paired"][f"v2_effect_A11v2-A11_{fset_name}"] = paired(report, "A11v2", "A11", qids)
    out = R5 / "reports" / "r5-score.json"
    out.parent.mkdir(exist_ok=True)
    slim = dict(report)
    slim["cases"] = {f"{a}|r{r}|{q}": {k: v for k, v in c.items() if k != "ledger"}
                     for (a, r, q), c in report["cases"].items()}
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("report ->", out)
    for arm, s in report["arm_summary"].items():
        print(f"{arm:6s} C={s['correct']:2d}/{s['runs']} unk={s['unknown']:2d} J2={s['j2']:2d} J4={s['j4']:2d} "
              f"meanS={s['mean_s_agent']}")
    for k, p in report["paired"].items():
        if isinstance(p, dict) and "mean" in p:
            print(f"{k}: {p['mean']:+.3f} CI95[{p['ci95'][0]:+.3f},{p['ci95'][1]:+.3f}] "
                  f"W{p['wins']}/L{p['losses']}/T{p['ties']}")
    log({"event": "score", "arms": sorted(report["arm_summary"])})
    return report


def monitor() -> dict:
    """Fixed observation layer: real-field reads + typed evidence per run.

    Replaces H4 monitor's broken `workflow_action.action.args.sql` reads
    (which produced cast_in_final_sql:null / sql_spans:[] for every case).
    Final SQL binds via result.output.sql_query_final; executed spans come
    from reconcile's result.sql_results; delivery evidence from the arm
    definition + per-run digest match.
    """
    out = {}
    for arm in ARMS:
        rows = []
        for r in (1, 2, 3):
            if not (R5 / "evidence" / f"r{r}-{arm}").exists():
                continue
            for c in score_run(arm, r):
                final_sql = ((c.get("final_sql") or "") or "")
                rows.append({
                    "qid": c["qid"], "rep": r, "family": c["family"],
                    "correct": c["correct"], "failure_type": c.get("failure_type"),
                    "s_agent": c.get("s_agent"),
                    "exp10_in_common": bool(ARMS[arm]["exp10"]),
                    "exp11_in_common": bool(ARMS[arm]["exp11"]),
                    "contract_delivered": c.get("contract_delivered"),
                    "final_sql_head": final_sql[:200] if final_sql else None,
                    "cast_in_final_sql": ("CAST(" in final_sql.upper()) if final_sql else None,
                    "inner_cast_avg": ("AVG(CAST(" in final_sql.upper().replace(" ", "")) if final_sql else None,
                    "coalesce_zero_around_agg": bool(exp10_admission.admission(final_sql).get("guards")) if final_sql else None,
                })
        out[arm] = rows
    path = R5 / "reports" / "r5-monitor.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("monitor ->", path)
    return out


def usage_from_trace(trace_path: Path) -> dict:
    last_cum = None
    fallback_req = fallback_in = fallback_out = 0
    used_fallback = False
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        act = rec.get("action", {}) if isinstance(rec.get("action"), dict) else {}
        if rec.get("kind") == "action_received" and act.get("action_type") == "token_usage":
            last_cum = act.get("output", {}).get("cumulative")
        for holder in (rec, act, act.get("output") if isinstance(act.get("output"), dict) else {}):
            if isinstance(holder, dict) and "input_tokens" in holder and "output_tokens" in holder \
                    and not isinstance(holder.get("input_tokens"), dict) and last_cum is None:
                fallback_req += 1
                fallback_in += holder.get("input_tokens") or 0
                fallback_out += holder.get("output_tokens") or 0
                used_fallback = True
    if last_cum:
        return {"requests": last_cum.get("requests", 0), "input_tokens": last_cum.get("input_tokens", 0),
                "output_tokens": last_cum.get("output_tokens", 0), "fallback": False}
    return {"requests": fallback_req, "input_tokens": fallback_in,
            "output_tokens": fallback_out, "fallback": used_fallback}


def cost() -> dict:
    per_arm, total = {}, {"runs": 0, "requests_started": 0, "requests_with_usage": 0,
                          "input_tokens": 0, "output_tokens": 0}
    for arm in ARMS:
        agg = {"runs": 0, "requests_started": 0, "requests_with_usage": 0, "missing_usage_requests": 0,
               "input_tokens": 0, "output_tokens": 0}
        for r in (1, 2, 3):
            out_root = R5 / "evidence" / f"r{r}-{arm}"
            if not out_root.exists():
                continue
            for qdir in sorted(p for p in out_root.iterdir() if p.is_dir()):
                tj = qdir / "trace.jsonl"
                if not tj.exists():
                    continue
                agg["runs"] += 1
                txt = tj.read_text(encoding="utf-8")
                started = txt.count('"kind": "model_started"') + txt.count('"kind":"model_started"')
                agg["requests_started"] += started
                u = usage_from_trace(tj)
                agg["requests_with_usage"] += u["requests"]
                agg["input_tokens"] += u["input_tokens"]
                agg["output_tokens"] += u["output_tokens"]
                agg["missing_usage_requests"] += max(0, started - u["requests"])
        per_arm[arm] = agg
        for k in ("runs", "requests_started", "requests_with_usage", "input_tokens", "output_tokens"):
            total[k] += agg[k]
        total["missing_usage_requests"] = total.get("missing_usage_requests", 0) + agg["missing_usage_requests"]
    out = {"per_arm": per_arm, "total": total, "budget": BUDGET,
           "note": "input/output tokens are the recorded-usage lower bound; requests_started minus "
                   "requests_with_usage are timed-out requests without final usage (disclosed, not zero-filled)"}
    path = R5 / "reports" / "r5-cost.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(total, indent=1))
    return out


def verdict() -> dict:
    """Preregistered verdicts: C-judgment routing + EXP-10 benefit + better-result."""
    report = json.loads((R5 / "reports" / "r5-score.json").read_text(encoding="utf-8"))
    v: dict = {}

    def p(name):
        return report["paired"][name]

    # 1) interference judgment (C routing) on non-precision questions
    e1 = p("EXP10_effect_A10-A00_non_precision")
    e2 = p("EXP10_effect_A11-A01_non_precision")
    # typed attribution: failures appearing with EXP-10 that are typed as
    # null_vs_zero / null_semantics / precision-family patterns on semantic questions
    typed = []
    for (arm, r, qid) in [(a, r, q) for a in ("A10", "A11") for r in (1, 2, 3) for q in NON_PRECISION]:
        c = report["cases"].get(f"{arm}|r{r}|{qid}")
        if c and c.get("correct") is False and c.get("failure_type") in ("null_vs_zero", "null_semantics"):
            base_arm = "A00" if arm == "A10" else "A01"
            b = report["cases"].get(f"{base_arm}|r{r}|{qid}")
            if not (b and b.get("correct") is True):
                typed.append(f"{arm} r{r} {qid} ({c['failure_type']})")
    neg_both = e1["mean"] < 0 and e2["mean"] < 0
    consistent = (e1["losses"] > e1["wins"]) and (e2["losses"] > e2["wins"])
    v["interference_supported"] = bool(neg_both and consistent and typed)
    v["interference_evidence"] = {"A10-A00": {k: e1[k] for k in ("mean", "ci95", "wins", "losses", "ties")},
                                  "A11-A01": {k: e2[k] for k in ("mean", "ci95", "wins", "losses", "ties")},
                                  "typed_attributed_failures": typed}
    v["c_routing_recommendation"] = ("B (conditional delivery)" if v["interference_supported"]
                                     else "C (structure experience expansion) — no controlled "
                                          "interference evidence; A案 cards stay unconditional")

    # 2) EXP-10 benefit retention on precision family
    b1 = p("EXP10_effect_A10-A00_precision")
    b2 = p("EXP10_effect_A11-A01_precision")
    v["exp10_benefit_retained"] = bool(b1["mean"] > 0 and b2["mean"] > 0)
    v["exp10_benefit_evidence"] = {"A10-A00": {k: b1[k] for k in ("mean", "ci95", "wins", "losses")},
                                   "A11-A01": {k: b2[k] for k in ("mean", "ci95", "wins", "losses")}}

    # 3) better-result branch rule (standing authorization: push branch, no PR/merge)
    g_all = p("v2_effect_A11v2-A11_all12")
    g_prec = p("v2_effect_A11v2-A11_precision")
    g_nonp = p("v2_effect_A11v2-A11_non_precision")
    v2_typed_regressions = []
    for r in (1, 2, 3):
        for qid in NON_PRECISION:
            c = report["cases"].get(f"A11v2|r{r}|{qid}")
            b = report["cases"].get(f"A11|r{r}|{qid}")
            if c and b and c.get("correct") is False and b.get("correct") is True \
                    and c.get("failure_type") in ("null_vs_zero", "null_semantics"):
                v2_typed_regressions.append(f"r{r} {qid}")
    v["better_result_conditions"] = {
        "noninferior_all12": g_all["mean"] >= 0,
        "precision_improved": g_prec["mean"] > 0,
        "nonprecision_mean_ge_minus_0.5": g_nonp["mean"] >= -0.5,
        "no_typed_regressions": not v2_typed_regressions,
        "evidence": {"all12": {k: g_all[k] for k in ("mean", "ci95")},
                     "precision": {k: g_prec[k] for k in ("mean", "ci95")},
                     "non_precision": {k: g_nonp[k] for k in ("mean", "ci95")},
                     "typed_regressions": v2_typed_regressions},
    }
    v["better_result"] = all(v["better_result_conditions"][k] for k in
                             ("noninferior_all12", "precision_improved",
                              "nonprecision_mean_ge_minus_0.5", "no_typed_regressions"))
    # delivery integrity gate
    dv = {a: d["delivery_violations"] for a, d in report["delivery"].items()}
    v["delivery_integrity_ok"] = not any(dv.values())
    v["delivery_violations"] = dv

    path = R5 / "reports" / "r5-verdict.json"
    path.write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(v, ensure_ascii=False, indent=1))
    log({"event": "verdict", "interference": v["interference_supported"],
         "benefit": v["exp10_benefit_retained"], "better": v["better_result"]})
    return v


def prereg() -> dict:
    distill_manifest = json.loads((R5 / "freeze" / "r5-distill-manifest.json").read_text(encoding="utf-8"))
    fixture_report = json.loads((R5 / "fixtures" / "fixture-battery-report.json").read_text(encoding="utf-8"))
    prereg_doc = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "round": "POWE-166 R5",
        "matrix": {
            "arms": {a: {"label": c["label"], "common_sha256": sha(c["common"]),
                         "exp10": c["exp10"], "exp11": c["exp11"]} for a, c in ARMS.items()},
            "arm_artifact_lineage": distill_manifest["arms"],
            "questions": ALLQ_IDS, "families": FAMILIES, "reps": REPS,
            "skills_dir": str(P2_SKILLS_DIR), "skill_names": P2_SKILLS, "inject": True,
            "contract_sha256": sha(CONTRACT), "contract_now_actually_delivered": True,
            "delivery_assert": "per-run result.delivered_common_sha256 must equal expected "
                               "sha256(arm_common + contract + answer protocol); violations void "
                               "same-field claims for that run",
        },
        "runtime": {
            "model": MODEL, "temperature": 0, "max_turns": MAX_TURNS, "timeout_s": TIMEOUT_S,
            "concurrency": CONCURRENCY, "current_date": CURRENT_DATE,
            "runner_sha256": sha(RUNNER), "runner_base": "run_qa_r4.py 895076bc + D1 contract payload fix "
                                                      "+ D2 r5patch + D3 delivery digests",
            "r5patch_sha256": sha(HERE / "r5patch.py"),
            "fixture_battery": {"failed": fixture_report["failed"], "cases": len(fixture_report["cases"])},
            "datus_runtime": str(RUNTIME_PY),
        },
        "metrics": {
            "J2": "correct AND S_agent<=2 (per family and overall; S_agent = online tool-call attempts)",
            "J4": "exploratory: correct AND S_agent<=4, reported for strict_structure/weighted_semantics families",
            "unknown": "stays in denominator; typed as timeout/max_turns/handoff_no_sql_context/final_binding/run_failed/other",
            "failure_typology": "incorrect runs typed precision_miss/null_vs_zero/null_semantics/semantic "
                                "(multiset-aligned cell comparison, rel<=1e-3 => precision)",
            "paired_unit": "rep-matched (question, rep) correctness diffs; 3 reps estimate stability, "
                           "not 3x independent samples",
        },
        "judgment_rules": {
            "interference_supported": "EXP-10 marginal effect (A10-A00 and A11-A01) on non-precision "
                                      "questions: mean<0 in BOTH, losses>wins in BOTH, AND >=1 typed "
                                      "null-family failure attributable to EXP-10 presence",
            "c_routing": "interference_supported -> recommend B (conditional delivery); else recommend "
                         "C (structure expansion priority); A案 cards remain unconditional meanwhile",
            "exp10_benefit_retained": "A10-A00 and A11-A01 mean>0 on precision family",
            "better_result_branch": "A11v2 vs A11: all-12 mean>=0 AND precision mean>0 AND non-precision "
                                    "mean>=-0.5 AND zero typed null-family regressions -> push branch "
                                    "swift/powe-166-r5 (standing authorization, no PR/no merge)",
            "no_post_hoc": "no metric, threshold, arm, or question changes after unblinding; unstable "
                           "results are reported as cannot-determine, no best-of reruns",
        },
        "budget": BUDGET,
        "known_limits": [
            "all 12 questions are exposed diagnostic material (H3/H4); no generalization claim",
            "A11 common == S4 champion bytes; its EXP-10 text was authored against V3-13/14 family "
            "(exposure bias disclosed)",
            "common-text length differs across arms (A00 shortest, A11/A11v2 longest); if EXP-10 "
            "differences appear, an equal-length placeholder control is the preregistered follow-up, "
            "not an in-round ad-hoc",
            "n=3 reps, 5-7 questions per family: low power; CI widths disclosed",
        ],
    }
    path = R5 / "freeze" / "prereg-r5.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(prereg_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    digest = sha(path)
    log({"event": "prereg_frozen", "sha256": digest, "path": str(path)})
    print("prereg frozen ->", path, digest[:12])
    return prereg_doc


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "prereg":
        prereg()
    elif cmd == "run":
        run_batch(sys.argv[2], int(sys.argv[3]))
    elif cmd == "matrix":
        arms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(ARMS)
        matrix(arms)
    elif cmd == "score":
        score()
    elif cmd == "monitor":
        monitor()
    elif cmd == "cost":
        cost()
    elif cmd == "verdict":
        verdict()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
