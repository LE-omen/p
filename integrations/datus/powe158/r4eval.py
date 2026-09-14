"""POWE-158 round-4 driver (Plan A mainline).

Stages (each frozen in r4/freeze/prereg-r4.json BEFORE its runs):
  a0      zero-model SQL canary (canary_a0.py, separate script)
  a1      24-run minimal factor experiment: {N, S3V2} x {old, full numeric
          contract} x {V3-13, V3-14} x 3 reps — confirms the causal direction
          of the unified numeric-contract fix (POWE-157 A1)
  distill self-distillation of the pre-aggregation precision experience from
          failure traces (distill.py, separate script) -> s4.txt artifact
  diag    exposure diagnostic round: {S4, P2, H0, N} (ALL with unified
          contract) x 12 exposed H3 questions x 3 reps = 144 runs
  r       champion old-R regression x3 (23 questions, stage line J>=14)

Frozen semantics: frozen lean runner (run_qa.py sha256 325f3750...) for old
delivery; run_qa_r4.py (same + contract_path append) for full-contract arms.
Scoring: powercontext_datus.scoring (byte-identical at champion HEAD
b17aa3af). unknown/timeout/failure stay in denominators; assembly-phase
failures (before question_injected) re-run <=3x and logged; DB host resolved
then pinned per batch. datus core agent loop untouched.

Usage (scoring venv python for plans/score; runs spawn the datus runtime venv):
  python r4eval.py a1plans | run <stage> <cond> <rep> | a1score
  python r4eval.py diagplans | diagscore
  python r4eval.py rplans <arm> | rscore <arm>
  python r4eval.py cost
"""
from __future__ import annotations

import hashlib
import json
import math
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
R4 = HERE.parent
W = Path("/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861")
P152 = W / "powe-152-4bed59acb45b" / "workdir"
H3E = W / "powe-155-30296b5c919c" / "workdir" / "h3eval"
PKG = H3E / "h3-package"
sys.path.insert(0, str(R4 / "powercontext" / "integrations" / "datus" / "src"))
sys.path.insert(0, str(R4 / "powercontext" / "src"))
from powercontext_datus.scoring import score_case, summarize_cases  # noqa: E402

RUNTIME_PY = P152 / "datus-runtime" / ".venv" / "bin" / "python"
RUNNER_FROZEN = HERE / "run_qa.py"
RUNNER_R4 = HERE / "run_qa_r4.py"
CONTRACT = R4 / "contract" / "numeric-contract-v1.md"

ART = P152 / "artifacts"
P2_SKILLS = ["d2-patterns", "legacy-patterns", "negative-guards"]
S3V2_COMMON = P152 / "r3" / "commons" / "s3v2.txt"

DB_HOSTNAME = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
MAX_TURNS, TIMEOUT_S, CONCURRENCY = 8, 420, 3
CURRENT_DATE = "2026-09-13"

ALLQ = {q["id"]: q for q in json.loads((PKG / "questions.json").read_text(encoding="utf-8"))["questions"]
        if q["partition"] == "H3"}
CANARY_Q = ["V3-13", "V3-14"]
# 12 exposed diagnostic questions (preregistered): the confirmed
# precision-failure family (13/14), its interpolation/weighted siblings
# (44/45), and 8 exposed non-family H3 controls covering non-temporal
# integer/decimal aggregates, inequality, OLS, indexes and log-odds.
DIAG_Q = ["V3-13", "V3-14", "V3-16", "V3-17", "V3-29", "V3-30",
          "V3-35", "V3-36", "V3-37", "V3-44", "V3-45", "V3-46"]
R_ORACLE = json.loads((HERE / "r-oracle.json").read_text(encoding="utf-8"))

A1_CONDITIONS = {
    "N-old":     {"skills_dir": ART / "empty-skills", "skill_names": [], "inject": False,
                  "common": HERE / "common-birdbench.txt", "runner": RUNNER_FROZEN, "contract": None},
    "N-full":    {"skills_dir": ART / "empty-skills", "skill_names": [], "inject": False,
                  "common": HERE / "common-birdbench.txt", "runner": RUNNER_R4, "contract": CONTRACT},
    "S3V2-old":  {"skills_dir": ART / "p2-additive", "skill_names": P2_SKILLS, "inject": True,
                  "common": S3V2_COMMON, "runner": RUNNER_FROZEN, "contract": None},
    "S3V2-full": {"skills_dir": ART / "p2-additive", "skill_names": P2_SKILLS, "inject": True,
                  "common": S3V2_COMMON, "runner": RUNNER_R4, "contract": CONTRACT},
}
DIAG_ARMS = {
    "S4": {"skills_dir": ART / "p2-additive", "skill_names": P2_SKILLS, "inject": True,
           "common": R4 / "artifacts" / "s4.txt", "runner": RUNNER_R4, "contract": CONTRACT},
    "P2": {"skills_dir": ART / "p2-additive", "skill_names": P2_SKILLS, "inject": True,
           "common": ART / "p2-additive-g1.txt", "runner": RUNNER_R4, "contract": CONTRACT},
    "H0": {"skills_dir": ART / "h0-skills",
           "skill_names": ["consumption-segment-aggregates", "customer-currency-metrics",
                           "customer-period-consumption", "gas-station-counts-ratios",
                           "transaction-lookups-joins"],
           "inject": True, "common": HERE / "common-birdbench.txt", "runner": RUNNER_R4, "contract": CONTRACT},
    "N":  {"skills_dir": ART / "empty-skills", "skill_names": [], "inject": False,
           "common": HERE / "common-birdbench.txt", "runner": RUNNER_R4, "contract": CONTRACT},
}

LINEAGE = R4 / "lineage.jsonl"


def log(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with LINEAGE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def resolve_db_ip() -> str:
    last = None
    for _ in range(6):
        try:
            return socket.gethostbyname(DB_HOSTNAME)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    raise SystemExit(f"cannot resolve {DB_HOSTNAME}: {last}")


def oracle_for(qid: str):
    o = json.loads((PKG / "oracle" / f"{qid}.json").read_text(encoding="utf-8"))
    return o["rows"], bool(ALLQ[qid].get("contract", {}).get("ordered", False))


def make_plan(stage: str, cond: str, rep: int, qids: list[str], cfg: dict, ip: str,
              out_root: Path, concurrency: int = CONCURRENCY) -> Path:
    qs = [{"qid": qid, "question": ALLQ[qid]["question"], "arm": cond, "task_id": qid,
           "skill_names": cfg["skill_names"], "inject_skills": cfg["inject"]}
          for qid in sorted(qids)]
    plan = {"run_id": f"powe158-{stage}-{cond.lower()}-r{rep}",
            "out_root": str(out_root),
            "db": {"host": ip, "port": 3306, "username": "mock_data_readonly", "name": "birdbench"},
            "db_hostname_resolved": {"hostname": DB_HOSTNAME, "ip": ip},
            "model": MODEL, "common_path": str(cfg["common"]),
            "contract_path": str(cfg["contract"]) if cfg.get("contract") else None,
            "skills_dir": str(cfg["skills_dir"]), "skill_names": cfg["skill_names"],
            "inject_skills": cfg["inject"], "max_turns": MAX_TURNS, "timeout_s": TIMEOUT_S,
            "current_date": CURRENT_DATE, "concurrency": concurrency,
            "condition_label": f"{stage}:{cond}", "questions": qs}
    out = R4 / "plans" / f"powe158-{stage}-{cond.lower()}-r{rep}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def make_r_plan(arm: str, rep: int, cfg: dict, ip: str, out_root: Path) -> Path:
    qs = [{"qid": str(q), "question": c["question"], "arm": arm, "task_id": str(q),
           "skill_names": cfg["skill_names"], "inject_skills": cfg["inject"]}
          for q, c in sorted(R_ORACLE.items(), key=lambda kv: int(kv[0]))]
    plan = {"run_id": f"powe158-r-{arm.lower()}-r{rep}", "out_root": str(out_root),
            "db": {"host": ip, "port": 3306, "username": "mock_data_readonly", "name": "birdbench"},
            "db_hostname_resolved": {"hostname": DB_HOSTNAME, "ip": ip},
            "model": MODEL, "common_path": str(cfg["common"]),
            "contract_path": str(cfg["contract"]) if cfg.get("contract") else None,
            "skills_dir": str(cfg["skills_dir"]), "skill_names": cfg["skill_names"],
            "inject_skills": cfg["inject"], "max_turns": MAX_TURNS, "timeout_s": TIMEOUT_S,
            "current_date": CURRENT_DATE, "concurrency": CONCURRENCY,
            "condition_label": f"r:{arm}", "questions": qs}
    out = R4 / "plans" / f"powe158-r-{arm.lower()}-r{rep}.json"
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def run_batch(stage: str, cond: str, rep: int, qids: list[str], cfg: dict,
              out_root: Path, concurrency: int = CONCURRENCY, runner: Path | None = None) -> None:
    runner = runner or cfg["runner"]
    for attempt in range(1, 4):
        plan_path = make_plan(stage, cond, rep, qids, cfg, resolve_db_ip(), out_root, concurrency)
        t0 = time.time()
        proc = subprocess.run([str(RUNTIME_PY), str(runner), "--batch", str(plan_path)],
                              cwd=str(HERE), capture_output=True, text=True)
        assembly_failed = []
        for qid in qids:
            rj = out_root / f"q{qid}" / "result.json"
            if rj.exists():
                res = json.loads(rj.read_text(encoding="utf-8"))
                if res.get("returncode") == 1 and res.get("steps") is None:
                    tj = rj.parent / "trace.jsonl"
                    injected = tj.exists() and "question_injected" in tj.read_text(encoding="utf-8")
                    if not injected:
                        assembly_failed.append(qid)
        log({"event": "exec", "stage": stage, "cond": cond, "rep": rep, "attempt": attempt,
             "rc": proc.returncode, "seconds": round(time.time() - t0, 1),
             "runner_sha256": sha(runner), "assembly_failed": assembly_failed})
        print(f"[{stage}-{cond}-r{rep}] attempt={attempt} rc={proc.returncode} "
              f"elapsed={time.time()-t0:.0f}s assembly_failed={assembly_failed}", flush=True)
        if proc.returncode != 0:
            print("STDERR tail:", (proc.stderr or "")[-500:], flush=True)
        if not assembly_failed:
            return
        for qid in assembly_failed:
            (out_root / f"q{qid}" / "result.json").unlink(missing_ok=True)
    print(f"WARNING: {stage} {cond} rep{rep} still has assembly failures after 3 attempts", flush=True)


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


def score_dir(out_root: Path, qids: list[str], oracle_fn) -> dict:
    cases = []
    for qid in qids:
        result, records = load_case(out_root / f"q{qid}")
        rows, ordered = oracle_fn(qid)
        case = score_case(result, records, rows, ordered=ordered)
        case["qid"] = qid
        case["arm"] = (result or {}).get("arm")
        case["wall_s"] = round(((result or {}).get("finished_at", 0) - (result or {}).get("started_at", 0)), 1)
        led = (result or {}).get("step_ledger") or {}
        case["ledger"] = {k: v for k, v in led.items() if k != "operations"}
        cases.append(case)
    return {"summary": summarize_cases(cases), "cases": cases}


def t_ci(diffs: list):
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n >= 2 else 0.0
    crit = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365,
            9: 2.306, 10: 2.262, 11: 2.228, 12: 2.201}.get(n, 1.96)
    half = crit * math.sqrt(var / n)
    return mean, mean - half, mean + half


def a1score() -> dict:
    report = {}
    for cond in A1_CONDITIONS:
        per_q = {}
        for r in (1, 2, 3):
            out_root = R4 / "evidence" / "a1" / f"{cond}-r{r}"
            rep = score_dir(out_root, CANARY_Q, oracle_for)
            for c in rep["cases"]:
                per_q.setdefault(c["qid"], {})[f"r{r}"] = {
                    "correct": c["correct"], "s_agent": c["s_agent"], "in_budget": c["in_budget"],
                    "unknown_reason": c.get("unknown_reason")}
        agg = {}
        for qid, runs in per_q.items():
            correct = sum(1 for v in runs.values() if v["correct"] is True)
            sv = [v["s_agent"] for v in runs.values() if v["s_agent"] is not None]
            agg[qid] = {"correct_of_3": correct,
                        "mean_s_agent": round(sum(sv) / len(sv), 2) if sv else None}
        report[cond] = {"per_run": per_q, "agg": agg,
                        "total_correct_of_6": sum(a["correct_of_3"] for a in agg.values())}
    # causal direction: full vs old per arm
    for arm in ("N", "S3V2"):
        qd = [report[f"{arm}-full"]["agg"][q]["correct_of_3"] - report[f"{arm}-old"]["agg"][q]["correct_of_3"]
              for q in CANARY_Q]
        mean, lo, hi = t_ci(qd) if len(qd) >= 2 else (None, None, None)
        report[f"causal_{arm}"] = {"per_q_diff": qd, "mean": mean, "ci95": [lo, hi]}
    out = R4 / "reports" / "a1-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: (v["total_correct_of_6"] if "total_correct_of_6" in v else v)
                      for k, v in report.items()}, indent=1))
    print("saved ->", out)
    return report


def diagscore() -> dict:
    report = {"arms": {}, "paired": {}}
    for arm in DIAG_ARMS:
        per_q = {}
        svals = []
        for r in (1, 2, 3):
            out_root = R4 / "evidence" / "diag" / f"{arm}-r{r}"
            rep = score_dir(out_root, DIAG_Q, oracle_for)
            for c in rep["cases"]:
                per_q.setdefault(c["qid"], {})[f"r{r}"] = {
                    "correct": c["correct"], "s_agent": c["s_agent"], "in_budget": c["in_budget"],
                    "unknown_reason": c.get("unknown_reason")}
                if c["s_agent"] is not None:
                    svals.append(c["s_agent"])
        agg = {}
        for qid, runs in per_q.items():
            correct = sum(1 for v in runs.values() if v["correct"] is True)
            inb = sum(1 for v in runs.values() if v["in_budget"] is True)
            unk = sum(1 for v in runs.values() if v["correct"] is None)
            sv = [v["s_agent"] for v in runs.values() if v["s_agent"] is not None]
            agg[qid] = {"correct_of_3": correct, "j_of_3": inb, "unknown_of_3": unk,
                        "mean_s_agent": round(sum(sv) / len(sv), 2) if sv else None}
        temporal = [q for q in ("V3-13", "V3-14", "V3-44", "V3-45")]
        controls = [q for q in DIAG_Q if q not in temporal]
        tot = lambda keys, f: sum(agg[q][f] for q in keys if q in agg)  # noqa: E731
        report["arms"][arm] = {
            "per_question": agg, "per_run": per_q,
            "meanC": round(sum(a["correct_of_3"] for a in agg.values()) / (len(DIAG_Q) * 3), 4),
            "total_correct_of_36": sum(a["correct_of_3"] for a in agg.values()),
            "j_rate": round(sum(a["j_of_3"] for a in agg.values()) / (len(DIAG_Q) * 3), 4),
            "temporal_correct_of_12": tot(temporal, "correct_of_3"),
            "control_correct_of_24": tot(controls, "correct_of_3"),
            "s_agent_mean": round(sum(svals) / len(svals), 2) if svals else None}
    for base in ("P2", "H0", "N"):
        qd = [report["arms"]["S4"]["per_question"][q]["correct_of_3"]
              - report["arms"][base]["per_question"][q]["correct_of_3"] for q in DIAG_Q]
        mean, lo, hi = t_ci(qd)
        report["paired"][f"S4_vs_{base}"] = {"per_q_diff": qd, "mean": round(mean, 4),
                                             "ci95": [round(lo, 4), round(hi, 4)]}
    out = R4 / "reports" / "diag-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for arm, a in report["arms"].items():
        print(f"{arm:3s} C/36={a['total_correct_of_36']:2d} meanC={a['meanC']:.3f} "
              f"J-rate={a['j_rate']:.3f} temporal/12={a['temporal_correct_of_12']} "
              f"control/24={a['control_correct_of_24']} meanS={a['s_agent_mean']}")
    for k, v in report["paired"].items():
        print(f"{k}: diff {v['mean']:+.3f} CI95 [{v['ci95'][0]:+.3f},{v['ci95'][1]:+.3f}]")
    print("saved ->", out)
    return report


def rscore(arm: str) -> dict:
    def r_oracle(qid):
        c = R_ORACLE[qid]
        return c["expected"]["rows"], bool(c.get("ordered"))
    report = {"per_run": {}, "agg": {}}
    for r in (1, 2, 3):
        out_root = R4 / "evidence" / "r" / f"{arm}-r{r}"
        rep = score_dir(out_root, [str(q) for q in sorted(R_ORACLE, key=int)], r_oracle)
        c = rep["summary"]
        report["per_run"][f"r{r}"] = {"correct": c.get("correct"), "j": c.get("j_agent_correct_and_within_2"),
                                      "unknown": c.get("unknown"), "mean_s_agent": c.get("mean_s_agent")}
        for case in rep["cases"]:
            report["agg"].setdefault(case["qid"], []).append(case["correct"])
    js = [v["j"] for v in report["per_run"].values() if v["j"] is not None]
    cs = [v["correct"] for v in report["per_run"].values() if v["correct"] is not None]
    report["meanJ"] = round(sum(js) / len(js), 2) if js else None
    report["meanC"] = round(sum(cs) / len(cs), 2) if cs else None
    out = R4 / "reports" / f"r-{arm}-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(arm, "per_run:", report["per_run"], "meanJ:", report["meanJ"], "meanC:", report["meanC"])
    return report


def usage_from_trace(trace_path: Path) -> dict:
    """Cost per run INCLUDING failures: last cumulative token_usage, else sum
    of per-request usage records (POWE-157 gap: unknown runs had 24 uncounted
    requests because malformed ledgers zeroed)."""
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
        # fallback: any record carrying per-request usage fields
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
    rows = []
    for stage, root in (("a1", R4 / "evidence" / "a1"), ("diag", R4 / "evidence" / "diag"),
                        ("r", R4 / "evidence" / "r")):
        if not root.exists():
            continue
        for batch in sorted(p for p in root.iterdir() if p.is_dir()):
            runs = requests = in_tok = out_tok = 0
            fallback_runs = 0
            for trace in sorted(batch.glob("q*/trace.jsonl")):
                runs += 1
                u = usage_from_trace(trace)
                requests += u["requests"]
                in_tok += u["input_tokens"]
                out_tok += u["output_tokens"]
                fallback_runs += 1 if u["fallback"] else 0
            rows.append({"stage": stage, "batch": batch.name, "runs": runs, "requests": requests,
                         "input_tokens": in_tok, "output_tokens": out_tok, "fallback_runs": fallback_runs})
    total = {k: sum(x.get(k, 0) for x in rows) for k in ("runs", "requests", "input_tokens", "output_tokens")}
    out = {"rows": rows, "total": total,
           "note": "failure/unknown run usage fully counted (cumulative or per-request fallback)"}
    (R4 / "reports" / "r4-cost.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(total))
    for r in rows:
        if r["fallback_runs"]:
            print("fallback usage used in", r["batch"], r["fallback_runs"], "runs")
    return out


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "a1plans":
        ip = resolve_db_ip()
        for rep in (1, 2, 3):  # interleaved: rep-major
            for cond, cfg in A1_CONDITIONS.items():
                print(make_plan("a1", cond, rep, CANARY_Q, cfg, ip,
                                R4 / "evidence" / "a1" / f"{cond}-r{rep}", concurrency=2))
    elif cmd == "a1run":
        for rep in (1, 2, 3):
            for cond, cfg in A1_CONDITIONS.items():
                run_batch("a1", cond, rep, CANARY_Q, cfg, R4 / "evidence" / "a1" / f"{cond}-r{rep}",
                          concurrency=2)
    elif cmd == "a1score":
        a1score()
    elif cmd == "diagplans":
        ip = resolve_db_ip()
        for rep in (1, 2, 3):
            for arm, cfg in DIAG_ARMS.items():
                print(make_plan("diag", arm, rep, DIAG_Q, cfg, ip, R4 / "evidence" / "diag" / f"{arm}-r{rep}"))
    elif cmd == "diagrun":
        for rep in (1, 2, 3):
            for arm, cfg in DIAG_ARMS.items():
                run_batch("diag", arm, rep, DIAG_Q, cfg, R4 / "evidence" / "diag" / f"{arm}-r{rep}")
    elif cmd == "diagscore":
        diagscore()
    elif cmd == "rplans":
        arm = sys.argv[2]
        cfg = DIAG_ARMS[arm]
        ip = resolve_db_ip()
        for rep in (1, 2, 3):
            print(make_r_plan(arm, rep, cfg, ip, R4 / "evidence" / "r" / f"{arm}-r{rep}"))
    elif cmd == "rrun":
        arm = sys.argv[2]
        cfg = DIAG_ARMS[arm]
        for rep in (1, 2, 3):
            plan_path = make_r_plan(arm, rep, cfg, resolve_db_ip(), R4 / "evidence" / "r" / f"{arm}-r{rep}")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for attempt in range(1, 4):
                t0 = time.time()
                proc = subprocess.run([str(RUNTIME_PY), str(cfg["runner"]), "--batch", str(plan_path)],
                                      cwd=str(HERE), capture_output=True, text=True)
                out_root = R4 / "evidence" / "r" / f"{arm}-r{rep}"
                assembly_failed = []
                for q in plan["questions"]:
                    rj = out_root / f"q{q['qid']}" / "result.json"
                    if rj.exists():
                        res = json.loads(rj.read_text(encoding="utf-8"))
                        if res.get("returncode") == 1 and res.get("steps") is None:
                            tj = rj.parent / "trace.jsonl"
                            if not (tj.exists() and "question_injected" in tj.read_text(encoding="utf-8")):
                                assembly_failed.append(q["qid"])
                log({"event": "exec", "stage": "r", "cond": arm, "rep": rep, "attempt": attempt,
                     "rc": proc.returncode, "seconds": round(time.time() - t0, 1),
                     "assembly_failed": assembly_failed})
                print(f"[r-{arm}-r{rep}] attempt={attempt} rc={proc.returncode} "
                      f"elapsed={time.time()-t0:.0f}s assembly_failed={assembly_failed}", flush=True)
                if not assembly_failed:
                    break
                for qid in assembly_failed:
                    (out_root / f"q{qid}" / "result.json").unlink(missing_ok=True)
    elif cmd == "rscore":
        rscore(sys.argv[2])
    elif cmd == "cost":
        cost()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
