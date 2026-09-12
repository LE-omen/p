"""POWE-138 ARM-C screening driver: build plans for each candidate, run the
lean runner sequentially (concurrency 3 inside), score with the frozen unified
scorer, and append a screening record. Usage:

  python drive_screen.py d5   # 4 candidates x screen_5 questions
  python drive_screen.py d10 C1 C3   # top-2 candidates x screen_10 questions
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from score_runs import EMPTY, d_questions, make_plan, score_run  # noqa: E402

REG = json.loads((HERE / "candidates" / "candidate-registry.json").read_text(encoding="utf-8"))
RUNNER_PY = "/home/rongfeng.frf/.multica/workspaces/powercontex-948fa5029861/powe-132-bce45073f992/workdir/powercontext/integrations/datus/runtime/.venv/bin/python"


def candidate_config(cid: str) -> dict:
    p = REG["candidates"][cid]
    if "common_override" in p:
        return {"skills_dir": str(HERE / "empty-skills"), "skill_names": [], "inject": False,
                "common": p["common_override"]}
    return {"skills_dir": p["skill_root"], "skill_names": p["skill_names"], "inject": True,
            "common": str(HERE / "common-birdbench.txt")}


def run_batch(run_id: str, cid: str, questions: list[dict], out_root: Path) -> int:
    cfg = candidate_config(cid)
    plan = make_plan(run_id, cid, questions, out_root, skills_dir=cfg["skills_dir"],
                     skill_names=cfg["skill_names"], inject=cfg["inject"], common=cfg["common"])
    proc = subprocess.run([RUNNER_PY, str(HERE / "run_qa.py"), "--batch", str(plan)],
                          cwd=str(HERE), capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-3:]
    print(f"[{run_id}] rc={proc.returncode} " + " | ".join(tail), flush=True)
    return proc.returncode


def screen(stage: str, cids: list[str]) -> None:
    n = 5 if stage == "d5" else 10
    questions = d_questions(n)
    log_path = HERE / "candidates" / f"screening-{stage}.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"stage": stage, "runs": {}}
    for cid in cids:
        run_id = f"powe138-{stage}-{cid.lower()}"
        out_root = HERE / "evidence" / f"{stage}-{cid}"
        rc = run_batch(run_id, cid, questions, out_root)
        report = score_run(out_root)
        log["runs"][cid] = {"run_id": run_id, "runner_rc": rc, "summary": report["summary"],
                            "per_case": [{"qid": c["qid"], "correct": c["correct"], "s_agent": c["s_agent"],
                                          "in_budget": c["in_budget"], "unknown_reason": c.get("unknown_reason")}
                                         for c in report["cases"]]}
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        s = report["summary"]
        print(f"[{cid}] C={s['correct']}/{s['total']} J={s['j_agent_correct_and_within_2']} "
              f"meanS={s['mean_s_agent']} unknown={s['unknown']}", flush=True)


if __name__ == "__main__":
    stage = sys.argv[1]
    cids = sys.argv[2:] or ["C1", "C2", "C3", "C4"]
    screen(stage, cids)
