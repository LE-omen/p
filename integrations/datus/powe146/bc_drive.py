"""POWE-146 portfolio iteration driver (B/C continuation + relaxed boundary).

Adapted from the POWE-145 bc_drive: candidate execution, scoring and lineage.

Commands:
  prep-cand  --cand NAME --gen N [--skills a,b] [--skills-src DIR] [--extk FILE] [--note "..." ]
  exec       --cand NAME --gen N --questions screen5|screen10|d23|r [--tag TAG]
  score      --cand NAME --gen N --questions ... [--tag TAG]
  summarize  [--gen N]

Runs the frozen lean runner (run_qa.py) under the pinned Datus runtime and
scores every case with the repo's unified scorer (powercontext_datus.scoring)
under the preregistered comparator. Candidate definitions live in
bc/candidates.json; every generation records lineage into bc/lineage.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parent
WORK = EXP.parent
REPO = WORK / "powercontext"
BC = WORK / "bc"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "integrations" / "datus" / "src"))

from powercontext_datus.scoring import score_case, summarize_cases  # noqa: E402

DB = {
    "host": "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com",
    "port": 3306,
    "username": "mock_data_readonly",
    "name": "birdbench",
}
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
MAX_TURNS = 8
TIMEOUT_S = 420
CONCURRENCY = 3
CURRENT_DATE = "2026-09-13"
RUNTIME_PYTHON = Path(
    "/home/rongfeng.frf/multica_workspaces/powercontex-948fa5029861/powe-132-bce45073f992/workdir/powercontext/integrations/datus/runtime/.venv/bin/python"
)

CANDIDATES = BC / "candidates.json"
LINEAGE = BC / "lineage.jsonl"
R_ORACLE = EXP / "r-oracle.json"
D2_ORACLE = REPO / "integrations" / "datus" / "e2" / "oracle-d2.json"
COMMON_BASE = (EXP / "common-birdbench.txt").read_text(encoding="utf-8")


def sha256_dir(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def log_lineage(event: dict) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    with LINEAGE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_candidates() -> dict:
    return json.loads(CANDIDATES.read_text(encoding="utf-8"))


def question_set(name: str) -> tuple[list[dict], dict]:
    if name in {"screen5", "screen10", "d23"}:
        oracle = json.loads(D2_ORACLE.read_text(encoding="utf-8"))
        manifest = json.loads((BC / "d2" / "manifest.json").read_text(encoding="utf-8"))
        ids = manifest["screen_5"] if name == "screen5" else manifest["screen_10"] if name == "screen10" else manifest["question_ids"]
        return [{"qid": qid, "question": oracle[qid]["question"]} for qid in ids], oracle
    if name == "r":
        oracle = json.loads(R_ORACLE.read_text(encoding="utf-8"))
        return (
            [{"qid": str(qid), "question": case["question"]} for qid, case in sorted(oracle.items(), key=lambda kv: int(kv[0]))],
            oracle,
        )
    raise SystemExit(f"unknown question set {name}")


def prep_candidate(options: argparse.Namespace) -> None:
    registry = load_candidates()
    skills = [name for name in (options.skills or "").split(",") if name]
    skills_dir = BC / "skills" / options.cand
    if skills_dir.exists():
        shutil.rmtree(skills_dir)
    skills_dir.mkdir(parents=True)
    source = BC / options.skills_src if options.skills_src else BC / f"gen{options.gen}"
    for skill in skills:
        shutil.copytree(source / skill, skills_dir / skill)
    common_path = EXP / "common-birdbench.txt"
    if options.extk:
        digest_text = (BC / options.extk).read_text(encoding="utf-8")
        common_path = BC / "common" / f"{options.cand}-g{options.gen}.txt"
        common_path.parent.mkdir(parents=True, exist_ok=True)
        common_path.write_text(COMMON_BASE + "\n\n" + digest_text, encoding="utf-8")
    registry[options.cand] = {
        "generation": options.gen,
        "note": options.note or "",
        "skills": skills,
        "skills_dir": str(skills_dir),
        "common_path": str(common_path),
        "skills_sha256": sha256_dir(skills_dir),
        "common_sha256": sha256_file(common_path),
        "inject": bool(options.inject or not options.extk),
    }
    CANDIDATES.write_text(json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8")
    log_lineage({
        "event": "prep",
        "cand": options.cand,
        "gen": options.gen,
        "note": options.note or "",
        "skills": skills,
        "skills_sha256": registry[options.cand]["skills_sha256"],
        "common_sha256": registry[options.cand]["common_sha256"],
    })
    print(json.dumps(registry[options.cand], indent=1))


def exec_batch(options: argparse.Namespace) -> None:
    registry = load_candidates()
    candidate = registry[options.cand]
    questions, _ = question_set(options.questions)
    tag = options.tag or f"g{options.gen}"
    out_root = BC / "runs" / options.cand / tag / options.questions
    plan = {
        "out_root": str(out_root),
        "run_id": f"{options.cand}-{tag}-{options.questions}-{int(time.time())}",
        "db": DB,
        "model": MODEL,
        "common_path": candidate["common_path"],
        "skills_dir": candidate["skills_dir"],
        "max_turns": MAX_TURNS,
        "timeout_s": TIMEOUT_S,
        "current_date": CURRENT_DATE,
        "concurrency": CONCURRENCY,
        "questions": [
            {
                **q,
                "arm": options.cand,
                "skill_names": candidate["skills"],
                "inject_skills": bool(candidate["inject"]),
            }
            for q in questions
        ],
    }
    plan_path = BC / "plans" / f"{options.cand}-{tag}-{options.questions}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    started = time.time()
    process = subprocess.run(
        [str(RUNTIME_PYTHON), str(EXP / "run_qa.py"), "--batch", str(plan_path)],
        cwd=str(EXP),
        capture_output=True,
        text=True,
    )
    log_lineage({
        "event": "exec",
        "cand": options.cand,
        "gen": options.gen,
        "tag": tag,
        "questions": options.questions,
        "out_root": str(out_root),
        "returncode": process.returncode,
        "seconds": round(time.time() - started, 1),
        "eval_head": subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip(),
    })
    print(process.stdout[-2000:])
    if process.returncode != 0:
        print(process.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"batch failed rc={process.returncode}")


def score_batch(options: argparse.Namespace) -> None:
    registry = load_candidates()
    tag = options.tag or f"g{options.gen}"
    _, oracle = question_set(options.questions)
    out_root = BC / "runs" / options.cand / tag / options.questions
    cases = []
    for qdir in sorted(p for p in out_root.iterdir() if p.is_dir()):
        qid = qdir.name[1:]
        result = json.loads((qdir / "result.json").read_text(encoding="utf-8"))
        records = [
            json.loads(line)
            for line in (qdir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        expected = oracle[qid]["expected"]["rows"]
        ordered = bool(oracle[qid].get("ordered"))
        case = score_case(result, records, expected, ordered=ordered)
        cases.append({
            "qid": qid,
            "correct": case["correct"],
            "unknown_reason": case.get("unknown_reason"),
            "s_agent": case["s_agent"],
            "in_budget": case.get("in_budget"),
            "final_sql": case.get("final_sql"),
            "ledger": case["ledger"],
            "error": result.get("error"),
            "returncode": result.get("returncode"),
        })
    summary = summarize_cases(cases)
    report = {"cand": options.cand, "gen": options.gen, "tag": tag, "questions": options.questions, "summary": summary, "cases": cases}
    report_path = BC / "reports" / f"{options.cand}-{tag}-{options.questions}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log_lineage({
        "event": "score",
        "cand": options.cand,
        "gen": options.gen,
        "tag": tag,
        "questions": options.questions,
        "correct": summary["correct"],
        "j_agent": summary["j_agent_correct_and_within_2"],
        "unknown": summary["unknown"],
        "mean_s_agent": summary["mean_s_agent"],
        "report": str(report_path),
    })
    print(json.dumps(summary, indent=1))
    for case in cases:
        print(f"{case['qid']}: correct={case['correct']} s={case['s_agent']} j={case['in_budget']} "
              f"unk={case['unknown_reason']} err={str(case['error'])[:80]}")



def cost_table(options: argparse.Namespace) -> None:
    """Aggregate token_usage records from every retained run trace into bc/cost-table.json."""
    rows = []
    for cand_dir in sorted(p for p in (BC / "runs").iterdir() if p.is_dir()):
        for tag_dir in sorted(p for p in cand_dir.iterdir() if p.is_dir()):
            for set_dir in sorted(p for p in tag_dir.iterdir() if p.is_dir()):
                runs = requests = in_tok = out_tok = 0
                for trace in sorted(set_dir.glob("q*/trace.jsonl")):
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
                if runs:
                    rows.append({"cand": cand_dir.name, "tag": tag_dir.name, "set": set_dir.name,
                                 "runs": runs, "requests": requests,
                                 "input_tokens": in_tok, "output_tokens": out_tok})
    total = {"runs": sum(r["runs"] for r in rows), "requests": sum(r["requests"] for r in rows),
             "input_tokens": sum(r["input_tokens"] for r in rows), "output_tokens": sum(r["output_tokens"] for r in rows)}
    (BC / "cost-table.json").write_text(json.dumps({"rows": rows, "total": total}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(total, indent=1))


def summarize(options: argparse.Namespace) -> None:
    registry = load_candidates()
    for path in sorted((BC / "reports").glob("*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if options.gen and report.get("gen") != options.gen:
            continue
        summary = report["summary"]
        print(
            f"{report['cand']:<22} g{report['gen']} {report['tag']:<8} {report['questions']:<8} "
            f"C={summary['correct']}/{summary['total']} J={summary['j_agent_correct_and_within_2']} "
            f"unk={summary['unknown']} meanS={summary['mean_s_agent']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prep-cand")
    prep.add_argument("--cand", required=True)
    prep.add_argument("--gen", type=int, required=True)
    prep.add_argument("--skills", default=None, help="comma-separated skill names from --skills-src")
    prep.add_argument("--skills-src", default=None)
    prep.add_argument("--extk", default=None, help="digest file (relative to bc/) rendered into external_knowledge")
    prep.add_argument("--inject", action="store_true", help="force required-skill injection even when --extk is set (portfolio packaging)")
    prep.add_argument("--note", default="")
    prep.set_defaults(func=prep_candidate)

    run = sub.add_parser("exec")
    run.add_argument("--cand", required=True)
    run.add_argument("--gen", type=int, required=True)
    run.add_argument("--questions", required=True)
    run.add_argument("--tag", default=None)
    run.set_defaults(func=exec_batch)

    score = sub.add_parser("score")
    score.add_argument("--cand", required=True)
    score.add_argument("--gen", type=int, required=True)
    score.add_argument("--questions", required=True)
    score.add_argument("--tag", default=None)
    score.set_defaults(func=score_batch)

    ccmd = sub.add_parser("cost")
    ccmd.set_defaults(func=cost_table)
    total = sub.add_parser("summarize")
    total.add_argument("--gen", type=int, default=None)
    total.set_defaults(func=summarize)

    options = parser.parse_args()
    options.func(options)


if __name__ == "__main__":
    main()
