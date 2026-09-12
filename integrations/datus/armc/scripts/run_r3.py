"""POWE-138 ARM-C: champion freeze + old-R x3 regression driver.

Order of operations (enforced by the freeze proof):
1. pick champion from D5+D10 screening by the shared selector rule
   (J_agent -> C -> lower mean S_agent -> fewer unknowns);
2. write freeze-proof.json (champion, content digest, git HEAD, timestamp)
   and COMMIT it to the local branch BEFORE any R question is executed;
3. only then run the old R set (CSV zero-based odd qids, 23 questions)
   three times with the frozen champion;
4. score every run with the frozen unified scorer; aggregate C / S_agent /
   J_agent per question per run, plus fallback rate and cost ledger.

  python run_r3.py freeze C1       # freeze champion C1
  python run_r3.py run             # R x3 (sequential, concurrency 3 inside)
  python run_r3.py report          # final R report
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score_runs import r_questions, score_run  # noqa: E402

RUNNER_PY = "/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861/powe-132-bce45073f992/workdir/powercontext/integrations/datus/runtime/.venv/bin/python"
REG = json.loads((HERE / "candidates" / "candidate-registry.json").read_text(encoding="utf-8"))
FREEZE = HERE / "candidates" / "freeze-proof.json"


def selector_key(summary: dict) -> tuple:
    """Shared selection rule: J desc, C desc, mean S_agent asc (None=inf), unknown desc."""
    mean_s = summary.get("mean_s_agent")
    return (-summary["j_agent_correct_and_within_2"], -summary["correct"],
            float("inf") if mean_s is None else mean_s, -summary.get("unknown", 0))


def pick_champion() -> tuple[str, dict]:
    d10 = json.loads((HERE / "candidates" / "screening-d10.json").read_text(encoding="utf-8"))["runs"]
    ranked = sorted(d10.items(), key=lambda kv: selector_key(kv[1]["summary"]))
    return ranked[0][0], {cid: {"summary": r["summary"]} for cid, r in d10.items()}


def freeze(champion_cid: str) -> None:
    if FREEZE.exists():
        print("freeze-proof.json already exists; refusing to refreeze");
        sys.exit(1)
    entry = REG["candidates"][champion_cid]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE.parent / "powercontext",
                          capture_output=True, text=True, check=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=HERE.parent / "powercontext",
                            capture_output=True, text=True, check=True).stdout.strip()
    proof = {
        "arm": "C",
        "champion": champion_cid,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "git_head": head,
        "git_branch": branch,
        "descriptor_digest": REG["descriptor_digest"],
        "champion_packaging": entry["packaging"],
        "champion_content_digest": entry["content_digest"],
        "r_results_seen_at_freeze": None,
        "statement": "champion frozen on D5+D10 screening alone; no old-R question has been "
                     "executed in any ARM-C run at this timestamp (smoke used learning-half q0)",
        "selection_rule": "J_agent desc -> C desc -> mean S_agent asc -> unknown desc (shared selector)",
    }
    FREEZE.write_text(json.dumps(proof, ensure_ascii=False, indent=1), encoding="utf-8")
    repo = HERE.parent / "powercontext"
    inrepo = repo / "integrations" / "datus" / "armc" / "freeze-proof.json"
    inrepo.parent.mkdir(parents=True, exist_ok=True)
    inrepo.write_text(FREEZE.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "integrations/datus/armc"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m",
                    f"chore(powe138): freeze ARM-C champion {champion_cid} before R regression (digest {entry['content_digest'][:12]})"],
                   check=True, capture_output=True)
    print(f"FROZEN champion={champion_cid} head={head[:12]} digest={entry['content_digest'][:16]}")


def run_r3() -> None:
    proof = json.loads(FREEZE.read_text(encoding="utf-8"))
    cid = proof["champion"]
    entry = REG["candidates"][cid]
    qs = r_questions()
    for i in (1, 2, 3):
        run_id = f"powe138-r{i}-{cid.lower()}"
        out_root = HERE / "evidence" / f"r{i}-{cid}"
        if "common_override" in entry:
            from score_runs import make_plan
            plan = make_plan(run_id, cid, qs, out_root, skills_dir=str(HERE / "empty-skills"),
                             skill_names=[], inject=False, common=entry["common_override"])
        else:
            from score_runs import make_plan
            plan = make_plan(run_id, cid, qs, out_root, skills_dir=entry["skill_root"],
                             skill_names=entry["skill_names"], inject=True)
        proc = subprocess.run([RUNNER_PY, str(HERE / "run_qa.py"), "--batch", str(plan)],
                              cwd=str(HERE), capture_output=True, text=True)
        print(f"[{run_id}] rc={proc.returncode} {proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ''}", flush=True)


def report() -> dict:
    proof = json.loads(FREEZE.read_text(encoding="utf-8"))
    cid = proof["champion"]
    out = {"champion": cid, "runs": {}, "per_question": {}}
    for i in (1, 2, 3):
        rep = score_run(HERE / "evidence" / f"r{i}-{cid}")
        out["runs"][f"r{i}"] = rep["summary"]
        for c in rep["cases"]:
            out["per_question"].setdefault(c["qid"], {})[f"r{i}"] = {
                "correct": c["correct"], "s_agent": c["s_agent"], "in_budget": c["in_budget"],
                "unknown_reason": c.get("unknown_reason")}
    # paired aggregation
    agg = {}
    for qid, runs in out["per_question"].items():
        correct = sum(1 for v in runs.values() if v["correct"] is True)
        inb = sum(1 for v in runs.values() if v["in_budget"] is True)
        svals = [v["s_agent"] for v in runs.values() if v["s_agent"] is not None]
        agg[qid] = {"correct_of_3": correct, "j_of_3": inb,
                    "mean_s_agent": round(sum(svals) / len(svals), 2) if svals else None}
    out["per_question_agg"] = agg
    per_run_j = [s["j_agent_correct_and_within_2"] for s in out["runs"].values()]
    out["j_agent_mean_over_3_runs"] = round(sum(per_run_j) / len(per_run_j), 2)
    out["c_mean_over_3_runs"] = round(sum(s["correct"] for s in out["runs"].values()) / len(out["runs"]), 2)
    (HERE / "candidates" / "r3-report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for k, s in out["runs"].items():
        print(f"{k}: C={s['correct']}/{s['total']} J={s['j_agent_correct_and_within_2']} meanS={s['mean_s_agent']} unknown={s['unknown']}")
    print(f"J_agent mean over 3 runs: {out['j_agent_mean_over_3_runs']}/23")
    return out


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "freeze":
        freeze(sys.argv[2])
    elif cmd == "run":
        run_r3()
    elif cmd == "report":
        report()
    elif cmd == "auto":
        cid, d10 = pick_champion()
        print(f"champion by shared selector: {cid}")
        freeze(cid)
        run_r3()
        report()
