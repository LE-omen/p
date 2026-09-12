"""POWE-137 ARM-B: build run plans for a candidate on a question set.

Question sets: d5 / d10 / d23 (D package slices), r1 / r2 / r3 (old-R repetitions),
d5-base (no-skill baseline on the D screening slice).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DB = {
    "host": "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com",
    "port": 3306,
    "username": "mock_data_readonly",
    "name": "birdbench",
}
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}


def load_d() -> list[dict]:
    questions = json.loads(Path(__file__).resolve().parent.parent.parent.joinpath("arm-b-data", "d-package", "questions.json").read_text(encoding="utf-8"))
    return questions["questions"] if isinstance(questions, dict) else questions


def d_ids(which: str) -> list[str]:
    root = Path(__file__).resolve().parent.parent.parent / "arm-b-data" / "d-package"
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if which == "d5":
        return list(m["screen_5"])
    if which == "d10":
        return list(m["screen_10"])
    return list(m["question_ids"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cand", required=True, help="candidate id (directory under armb/skills) or 'baseline'")
    parser.add_argument("--set", required=True, choices=["d5", "d10", "d23", "r1", "r2", "r3", "d5-base"])
    parser.add_argument("--skill-name", default="")
    parser.add_argument("--out", required=True)
    options = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    workdir = root.parent
    run_id = f"powe137-{options.cand}-{options.set}"

    set_name = options.set.removesuffix("-base")
    if options.set.startswith("r"):
        oracle = json.loads((root / "records" / "r-oracle.json").read_text(encoding="utf-8"))["expected"]
        questions = [
            {"qid": v["qid"], "question": v["question"], "arm": f"B/{options.cand}/{options.set}"}
            for v in oracle.values()
        ]
    else:
        d = {q["id"]: q for q in load_d()}
        questions = [
            {"qid": qid, "question": d[qid]["question"], "arm": f"B/{options.cand}/{options.set}"}
            for qid in d_ids(set_name)
        ]

    skills_dir = root / "empty-skills"
    skill_names: list[str] = []
    inject = False
    if options.cand != "baseline":
        skills_dir = root / "skills" / options.cand
        skill_names = [options.skill_name]
        inject = True

    plan = {
        "run_id": run_id,
        "db": DB,
        "model": MODEL,
        "common_path": str(root / "common-birdbench.txt"),
        "skills_dir": str(skills_dir),
        "skill_names": skill_names,
        "inject_skills": inject,
        "max_turns": 8,
        "timeout_s": 420,
        "current_date": "2026-09-12",
        "concurrency": 3,
        "out_root": str(root / "runs" / options.cand / options.set),
        "questions": [
            {**q, "skill_names": skill_names, "inject_skills": inject} for q in questions
        ],
    }
    Path(options.out).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"plan written: {options.out} ({len(questions)} questions, skills={skill_names or 'none'})")


if __name__ == "__main__":
    sys.exit(main())
