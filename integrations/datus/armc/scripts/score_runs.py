"""POWE-138 ARM-C: build run plans and score them with the frozen unified scorer.

Scoring uses powercontext_datus.scoring.score_case (frozen HEAD comparator:
column-count checked, row multiset preserving duplicates, ordered only when the
question contract demands it, strict final-SQL binding, unknowns kept).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "powercontext" / "integrations" / "datus" / "src"))
from powercontext_datus.scoring import score_case, summarize_cases  # noqa: E402

DB = {"host": "t7qse7zfed4cg-mi.cn-hangzhou.oceanbase.aliyuncs.com", "port": 3306,
      "username": "mock_data_readonly", "name": "birdbench"}
MODEL = {"model": "deepseek-v4-flash-0731", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
DQ = json.loads((HERE / "d-package" / "questions.json").read_text(encoding="utf-8"))["questions"]
COMMON = str(HERE / "common-birdbench.txt")
EMPTY = str(HERE / "empty-skills")


def make_plan(run_id: str, arm: str, questions: list[dict], out_root: Path, *, skills_dir: str,
              skill_names: list[str], inject: bool = True, common: str = COMMON,
              concurrency: int = 3) -> Path:
    qs = [dict(q, skill_names=skill_names, inject_skills=inject) for q in questions]
    plan = {"run_id": run_id, "db": DB, "model": MODEL, "common_path": common,
            "skills_dir": skills_dir, "skill_names": skill_names, "inject_skills": inject,
            "max_turns": 8, "timeout_s": 420, "current_date": "2026-09-12",
            "concurrency": concurrency, "out_root": str(out_root), "questions": qs}
    out = HERE / "plans" / f"{run_id}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def d_questions(screen: int) -> list[dict]:
    out = []
    for q in DQ:
        if q.get(f"screen_{screen}"):
            out.append({"qid": q["id"], "question": q["question"], "arm": f"d{screen}", "task_id": q["id"]})
    return out


def r_questions() -> list[dict]:
    oracle = json.loads((HERE / "r-oracle.json").read_text(encoding="utf-8"))["oracle"]
    return [{"qid": str(v["qid"]), "question": v["question"], "arm": "r", "task_id": f"q{v['qid']}"}
            for v in sorted(oracle.values(), key=lambda v: v["qid"])]


def load_case(qdir: Path) -> tuple[dict | None, list | None]:
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


def expected_rows(qid: str) -> tuple[list, bool]:
    """(expected_rows, ordered) — D contracts demand explicit order; old R is unordered."""
    if qid.startswith("N"):
        o = json.loads((HERE / "d-package" / "oracle" / f"{qid}.json").read_text(encoding="utf-8"))
        return o["result"]["rows"], True
    oracle = json.loads((HERE / "r-oracle.json").read_text(encoding="utf-8"))["oracle"]
    return oracle[str(int(qid))]["rows"], False


def score_run(out_root: Path) -> dict:
    cases = []
    for qdir in sorted(out_root.iterdir() if out_root.exists() else []):
        if not qdir.is_dir():
            continue
        result, records = load_case(qdir)
        rows, ordered = expected_rows(qdir.name[1:])
        case = score_case(result, records, rows, ordered=ordered)
        case["qid"] = qdir.name[1:]
        case["arm"] = (result or {}).get("arm")
        cases.append(case)
    summary = summarize_cases(cases)
    return {"summary": summary, "cases": cases}


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "score":
        out_root = Path(sys.argv[2])
        report = score_run(out_root)
        out = Path(sys.argv[3])
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        s = report["summary"]
        print(f"total={s['total']} correct={s['correct']} incorrect={s['incorrect']} unknown={s['unknown']} "
              f"J={s['j_agent_correct_and_within_2']} meanS={s['mean_s_agent']} dist={s['s_agent_distribution']}")
        for c in report["cases"]:
            print(f"  {c['qid']}: correct={c['correct']} s={c['s_agent']} reason={c.get('unknown_reason')}")
    elif cmd == "ledger":
        # aggregate secondary ledger (model requests/tokens/task-SQL) for cost table
        out_root = Path(sys.argv[2])
        agg = {}
        for qdir in sorted(out_root.iterdir() if out_root.exists() else []):
            if not qdir.is_dir():
                continue
            result, _ = load_case(qdir)
            if result is None:
                continue
            led = result.get("step_ledger") or {}
            agg[qdir.name[1:]] = {k: v for k, v in led.items() if k != "operations"}
        Path(sys.argv[3]).write_text(json.dumps(agg, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"ledger for {len(agg)} cases -> {sys.argv[3]}")


if __name__ == "__main__":
    main()
