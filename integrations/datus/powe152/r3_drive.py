"""Run under the datus runtime venv (project/exec); score under the scoring venv.

POWE-152 round-3 driver: S1 ledger + S2/S3 screenings + D23x3 + freeze + regressions.

Preregistered rules (frozen before any run this round; see r3/prereg.json):
  * selection order over finalists: D23x3 mean C desc -> mean J desc -> mean S_agent asc;
  * stage lines: D23x3 mean J >= 17/23 and old R x3 mean J >= 14/23;
  * "better result than p2-additive" (standing push-branch authorization) =
    champion H2x3 gate-13 correct strictly > p2 frozen 64.1% (25/39) while
    R x3 mean J >= 14 and D23x3 mean C >= p2 D23x3 mean C - 1;
  * unknown/timeout/failure stay in denominators; assembly-phase failures
    (connector/DNS before question_injected) are re-run and each retry logged;
  * H2 is EXPOSED regression only; H3 (POWE-153) untouched until frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parent
WORK = EXP.parent
REPO = WORK / "powercontext"
R3 = WORK / "r3"
sys.path.insert(0, str(REPO / "integrations" / "datus" / "src"))
sys.path.insert(0, str(REPO / "src"))


DB_HOSTNAME = "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com"
DB = {"host": DB_HOSTNAME, "port": 3306, "username": "mock_data_readonly", "name": "birdbench"}
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
MAX_TURNS = 8
TIMEOUT_S = 420
CONCURRENCY = 3
CURRENT_DATE = "2026-09-13"
RUNTIME_PY = WORK / "datus-runtime" / ".venv" / "bin" / "python"
RUNNER_FROZEN = EXP / "run_qa.py"
RUNNER_R3 = EXP / "run_qa_r3.py"

ART = WORK / "artifacts"
P2_SKILLS = ART / "p2-additive"
P2_COMMON = ART / "p2-additive-g1.txt"
COMMON_BASE = (EXP / "common-birdbench.txt").read_text(encoding="utf-8")
R_ORACLE = json.loads((EXP / "r-oracle.json").read_text(encoding="utf-8"))
D2_ORACLE = json.loads((REPO / "integrations" / "datus" / "e2" / "oracle-d2.json").read_text(encoding="utf-8"))
SCREEN_10 = ["V2-01", "V2-02", "V2-06", "V2-07", "V2-08", "V2-10", "V2-11", "V2-17", "V2-21", "V2-23"]
S2_R10 = ["1", "15", "21", "25", "27", "29", "31", "33", "35", "37"]
H2P = WORK / "h2-package"

LINEAGE = R3 / "lineage.jsonl"
SCOPE = {"datasource": "birdbench", "dialect": "mysql", "schema_fingerprint": "birdbench-debit_card_specializing-v2"}


def log(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with LINEAGE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("LINEAGE", json.dumps(event, ensure_ascii=False)[:200], flush=True)


def resolve_db_ip() -> str:
    last = None
    for _ in range(6):
        try:
            return socket.gethostbyname(DB_HOSTNAME)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    raise SystemExit(f"cannot resolve {DB_HOSTNAME}: {last}")


def questions_for(qset: str) -> tuple[list[dict], dict]:
    if qset == "s2r10":
        return ([{"qid": q, "question": R_ORACLE[q]["question"]} for q in S2_R10], R_ORACLE)
    if qset == "d10":
        return ([{"qid": q, "question": D2_ORACLE[q]["question"]} for q in SCREEN_10], D2_ORACLE)
    if qset == "d23":
        return ([{"qid": q, "question": v["question"]} for q, v in sorted(D2_ORACLE.items())], D2_ORACLE)
    if qset == "r":
        return ([{"qid": str(q), "question": c["question"]} for q, c in sorted(R_ORACLE.items(), key=lambda kv: int(kv[0]))], R_ORACLE)
    if qset == "h2":
        qs = json.loads((H2P / "questions.json").read_text(encoding="utf-8"))["questions"]
        oracle = {}
        for q in qs:
            o = json.loads((H2P / "oracle" / f"{q['id']}.json").read_text(encoding="utf-8"))
            oracle[q["id"]] = {"question": q["question"], "expected": {"rows": o["rows"]},
                               "ordered": bool(q.get("contract", {}).get("ordered", False))}
        return ([{"qid": q["id"], "question": q["question"]} for q in sorted(qs, key=lambda x: x["id"])], oracle)
    raise SystemExit(f"unknown question set {qset}")


def arm_config(arm: str) -> dict:
    """Condition wiring. Frozen arms use the frozen runner; S2 arms the r3 runner."""
    if arm in {"p2", "s3v1", "s3v2"}:
        common = {"p2": P2_COMMON, "s3v1": R3 / "commons" / "s3v1.txt", "s3v2": R3 / "commons" / "s3v2.txt"}[arm]
        return {"skills_dir": P2_SKILLS, "skill_names": ["d2-patterns", "legacy-patterns", "negative-guards"],
                "inject": True, "common": common, "runner": RUNNER_FROZEN, "store": None}
    if arm in {"s2render", "s2direct"}:
        common = R3 / "commons" / f"{arm}.txt"
        return {"skills_dir": P2_SKILLS, "skill_names": ["d2-patterns", "legacy-patterns", "negative-guards"],
                "inject": True, "common": common, "runner": RUNNER_R3,
                "store": {"enabled": True, "mode": "render" if arm == "s2render" else "direct"}}
    if arm == "n":
        return {"skills_dir": ART / "empty-skills", "skill_names": [], "inject": False,
                "common": EXP / "common-birdbench.txt", "runner": RUNNER_FROZEN, "store": None}
    raise SystemExit(f"unknown arm {arm}")


def build_commons() -> None:
    """Materialize S2/S3 common files + freeze manifests (before any run)."""
    from powercontext_datus import s2templates as s2
    from powercontext_datus import experience_entries as ee

    out = R3 / "commons"
    out.mkdir(parents=True, exist_ok=True)
    entries = s2.build_template_set()
    freeze = s2.freeze_template_set(entries)
    (R3 / "freeze" / "s2-template-set.json").parent.mkdir(parents=True, exist_ok=True)
    (R3 / "freeze" / "s2-template-set.json").write_text(json.dumps(freeze, indent=1))

    p2_text = P2_COMMON.read_text(encoding="utf-8")
    marker = "# STRUCTURED QUERY TEMPLATES"
    head = p2_text.split(marker)[0].rstrip("\n")
    tail = p2_text.split(marker, 1)[1]
    guards = tail.split("## Non-negotiable guards", 1)
    guards = "## Non-negotiable guards" + guards[1].split("## Templates", 1)[0] if len(guards) > 1 else guards[0]
    for mode in ("render", "direct"):
        tool_line = {
            "render": (
                "REFERENCE TEMPLATES are available as NATIVE TOOLS (search_reference_template / get_reference_template / render_reference_template).\n"
                "PREFERRED consumption: get_reference_template for the exact body+parameters, then render_reference_template with the bound\n"
                "parameter values (the tool renders server-side and validates types), then execute the rendered SQL with the native execute_sql\n"
                "and submit it as the final SQL. When a template matches the question, do not rewrite its SQL by hand; bind its parameters.\n"
                "If no template matches (check the index below), derive the SQL natively from the schema; the guards above still apply."
            ),
            "direct": (
                "REFERENCE TEMPLATES are available as NATIVE TOOLS (search_reference_template / get_reference_template / render_reference_template / execute_reference_template).\n"
                "PREFERRED consumption: execute_reference_template renders the template server-side with your bound parameters and executes the\n"
                "read-only SQL in one step, returning rendered_sql + query_result. Then submit that exact rendered_sql as the final SQL through\n"
                "the native flow. When a template matches the question, do not rewrite its SQL by hand; bind its parameters.\n"
                "If no template matches (check the index below), derive the SQL natively from the schema; the guards above still apply."
            ),
        }[mode]
        index = ["## Template index (frozen set, subject_path birdbench/*)", ""]
        for e in entries:
            params = "; ".join(f"{p['name']}({p['type']}{',opt' if not p.get('required') else ''})"
                               for p in e["parameters"])
            index.append(f"- {e['name']} [{e['family']}, hops={e['hops']}]: {e['summary']} Params: {params}. Guards: {', '.join(e.get('guards', []))}.")
        text = head + "\n\n" + marker + "\n" + guards + "\n" + tool_line + "\n\n" + "\n".join(index) + "\n"
        path = out / f"s2{mode}.txt"
        path.write_text(text, encoding="utf-8")

    for variant, builder in (("v1", ee.build_v1_entries), ("v2", ee.build_v2_entries)):
        es3 = builder()
        manifest = ee.freeze_experience(es3, variant=variant)
        (R3 / "freeze" / f"s3-experience-{variant}.json").write_text(json.dumps(manifest, indent=1))
        (out / f"s3{variant}.txt").write_text(ee.render_experience_digest(es3, base_common=p2_text), encoding="utf-8")

    log({"event": "build_commons", "s2_set": freeze["set_sha256"],
         "s3v1": manifest_of("v1"), "s3v2": manifest_of("v2")})
    print("commons built")


def manifest_of(variant: str) -> str:
    p = R3 / "freeze" / f"s3-experience-{variant}.json"
    return json.loads(p.read_text(encoding="utf-8"))["set_sha256"] if p.exists() else None


def make_plan(arm: str, qset: str, rep: int, out_root: Path) -> Path:
    cfg = arm_config(arm)
    qs, _ = questions_for(qset)
    ip = resolve_db_ip()
    plan = {
        "run_id": f"powe152-r3-{arm}-{qset}-rep{rep}-{int(time.time())}",
        "out_root": str(out_root),
        "db": {**DB, "host": ip},
        "db_hostname_resolved": {"hostname": DB_HOSTNAME, "ip": ip},
        "model": MODEL,
        "common_path": str(cfg["common"]),
        "skills_dir": str(cfg["skills_dir"]),
        "max_turns": MAX_TURNS, "timeout_s": TIMEOUT_S, "current_date": CURRENT_DATE,
        "concurrency": CONCURRENCY,
        "template_store": cfg["store"],
        "questions": [{**q, "arm": arm, "skill_names": cfg["skill_names"], "inject_skills": cfg["inject"]} for q in qs],
    }
    plans = R3 / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{arm}-{qset}-rep{rep}.json"
    path.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    return path


def project_store_for(qdir: Path) -> None:
    from powercontext_datus import s2templates as s2
    marker = qdir / ".store-projected"
    if marker.exists():
        return
    entries = s2.build_template_set()
    for e in entries:
        e.setdefault("id", e["name"])
    res = s2.project_store(qdir / "home" / ".datus", entries)
    marker.write_text(json.dumps(res))
    log({"event": "store_projected", "qdir": str(qdir), **res})


def run_batch(arm: str, qset: str, rep: int, retries: int = 3) -> None:
    cfg = arm_config(arm)
    out_root = R3 / "runs" / arm / f"{qset}-rep{rep}"
    qs, _ = questions_for(qset)
    if cfg["store"]:
        for q in qs:
            project_store_for(out_root / f"q{q['qid']}")
    plan_path = make_plan(arm, qset, rep, out_root)
    runner = cfg["runner"]
    for attempt in range(1, retries + 1):
        started = time.time()
        proc = subprocess.run([str(RUNTIME_PY), str(runner), "--batch", str(plan_path)],
                              cwd=str(EXP), capture_output=True, text=True)
        assembly_failed = []
        for q in qs:
            rj = out_root / f"q{q['qid']}" / "result.json"
            if rj.exists():
                r = json.loads(rj.read_text(encoding="utf-8"))
                if r.get("returncode") == 1 and r.get("steps") is None:
                    tj = rj.parent / "trace.jsonl"
                    injected = tj.exists() and "question_injected" in tj.read_text(encoding="utf-8")
                    if not injected:
                        assembly_failed.append(q["qid"])
        log({"event": "exec", "arm": arm, "qset": qset, "rep": rep, "attempt": attempt,
             "rc": proc.returncode, "seconds": round(time.time() - started, 1),
             "assembly_failed": assembly_failed,
             "runner": str(runner),
             "eval_head": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip()})
        if proc.returncode != 0:
            print(proc.stdout[-1500:], proc.stderr[-1500:], file=sys.stderr)
        if not assembly_failed:
            print(f"batch complete: {arm} {qset} rep{rep}")
            return
        for qid in assembly_failed:  # assembly-phase failure: re-run (never started)
            rj = out_root / f"q{qid}" / "result.json"
            if rj.exists():
                rj.unlink()
    print(f"WARNING: {arm} {qset} rep{rep} still has assembly failures after {retries} attempts")


def score_batch(arm: str, qset: str, rep: int) -> dict:
    from powercontext_datus.scoring import score_case, summarize_cases
    from powercontext_datus import delivery_ledger as dl
    cfg = arm_config(arm)
    _, oracle = questions_for(qset)
    out_root = R3 / "runs" / arm / f"{qset}-rep{rep}"
    contract = dl.contract_from_condition(
        label=arm, skills_dir=cfg["skills_dir"], skill_names=cfg["skill_names"],
        common_path=cfg["common"], scope=SCOPE)
    cases, ledgers = [], []
    for qdir in sorted(p for p in out_root.iterdir() if p.is_dir()):
        qid = qdir.name[1:]
        result = json.loads((qdir / "result.json").read_text(encoding="utf-8"))
        records = [json.loads(l) for l in (qdir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        expected = oracle[qid]["expected"]["rows"]
        ordered = bool(oracle[qid].get("ordered"))
        case = score_case(result, records, expected, ordered=ordered)
        cases.append({"qid": qid, "correct": case["correct"], "unknown_reason": case.get("unknown_reason"),
                      "s_agent": case["s_agent"], "in_budget": case.get("in_budget"),
                      "final_sql": case.get("final_sql"), "ledger": case["ledger"],
                      "error": result.get("error")})
        ledgers.append(dl.account_run(qdir, contract=contract, plan_common_path=cfg["common"]))
    summary = summarize_cases(cases)
    agg = dl.aggregate_ledgers(ledgers)
    reports = R3 / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report = {"arm": arm, "qset": qset, "rep": rep, "summary": summary, "cases": cases,
              "delivery": agg, "bundle_id": contract["bundle_id"]}
    (reports / f"{arm}-{qset}-rep{rep}.json").write_text(json.dumps(report, default=str, indent=1))
    (R3 / "ledgers").mkdir(parents=True, exist_ok=True)
    (R3 / "ledgers" / f"{arm}-{qset}-rep{rep}.json").write_text(json.dumps(ledgers, default=str, indent=1))
    log({"event": "score", "arm": arm, "qset": qset, "rep": rep,
         "correct": summary["correct"], "total": summary["total"],
         "j_agent": summary["j_agent_correct_and_within_2"], "unknown": summary["unknown"],
         "mean_s_agent": summary["mean_s_agent"]})
    print(f"{arm} {qset} rep{rep}: C={summary['correct']}/{summary['total']} "
          f"J={summary['j_agent_correct_and_within_2']} unk={summary['unknown']} meanS={summary['mean_s_agent']}")
    return report


def cost_of(arm: str, qset: str, rep: int) -> dict:
    out_root = R3 / "runs" / arm / f"{qset}-rep{rep}"
    runs = requests = in_tok = out_tok = 0
    for trace in sorted(out_root.glob("q*/trace.jsonl")):
        runs += 1
        last = None
        for line in trace.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") == "action_received" and rec.get("action", {}).get("action_type") == "token_usage":
                last = rec["action"].get("output", {}).get("cumulative")
        if last:
            requests += last.get("requests", 0)
            in_tok += last.get("input_tokens", 0)
            out_tok += last.get("output_tokens", 0)
    return {"runs": runs, "requests": requests, "input_tokens": in_tok, "output_tokens": out_tok}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-commons")
    p_run = sub.add_parser("run")
    p_run.add_argument("--arm", required=True)
    p_run.add_argument("--qset", required=True)
    p_run.add_argument("--rep", type=int, default=1)
    p_run.add_argument("--retries", type=int, default=3)
    p_score = sub.add_parser("score")
    p_score.add_argument("--arm", required=True)
    p_score.add_argument("--qset", required=True)
    p_score.add_argument("--rep", type=int, default=1)
    sub.add_parser("cost")
    args = parser.parse_args()
    if args.cmd == "build-commons":
        build_commons()
    elif args.cmd == "run":
        run_batch(args.arm, args.qset, args.rep, args.retries)
    elif args.cmd == "score":
        score_batch(args.arm, args.qset, args.rep)
    elif args.cmd == "cost":
        rows = []
        for arm_dir in sorted((R3 / "runs").iterdir()):
            if not arm_dir.is_dir():
                continue
            for set_dir in sorted(arm_dir.iterdir()):
                if set_dir.name.count("-rep") != 1:
                    continue
                qset, rep = set_dir.name.rsplit("-rep", 1)
                rows.append({"arm": arm_dir.name, "qset": qset, "rep": int(rep), **cost_of(arm_dir.name, qset, int(rep))})
        total = {k: sum(r.get(k, 0) for r in rows) for k in ("runs", "requests", "input_tokens", "output_tokens")}
        (R3 / "cost.json").write_text(json.dumps({"rows": rows, "total": total}, indent=1))
        print(json.dumps(total))


if __name__ == "__main__":
    main()
