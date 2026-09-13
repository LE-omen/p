"""POWE-152 selection, freeze and final-eval aggregation (run under scoring venv)."""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

EXP = Path(__file__).resolve().parent
WORK = EXP.parent
REPO = WORK / "powercontext"
R3 = WORK / "r3"
sys.path.insert(0, str(REPO / "integrations" / "datus" / "src"))

GATE13 = ["V2-13", "V2-24", "V2-28", "V2-30", "V2-33", "V2-34", "V2-35", "V2-36",
          "V2-40", "V2-41", "V2-42", "V2-43", "V2-44"]
P2_H2_FROZEN_GATE = {"correct": 25, "of": 39, "accuracy": 0.641, "j": 18, "j_rate": 0.4615}
P2_R_FROZEN = {"mean_j": 18.0}
P2_D23_SINGLE = {"c": 23, "j": 19}


def report(arm, qset, rep) -> dict | None:
    p = R3 / "reports" / f"{arm}-{qset}-rep{rep}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def screen() -> None:
    print("== S2 consumption comparison (s2r10 x1, same C4 content) ==")
    for arm in ("p2", "s2render", "s2direct"):
        r = report(arm, "s2r10", 1)
        if not r:
            print(f"{arm:<10} (pending)")
            continue
        s = r["summary"]
        d = r["delivery"]
        print(f"{arm:<10} C={s['correct']}/{s['total']} J={s['j_agent_correct_and_within_2']} "
              f"unk={s['unknown']} meanS={round(s['mean_s_agent'],2)} "
              f"bytes~{d['mean_delivered_bytes']:.0f} estTok~{d['mean_estimated_tokens']:.0f} "
              f"req={d['total_actual_requests']}")
    print("== S3 content increment (d10 x1, p2 base) ==")
    for arm in ("p2", "s3v1", "s3v2"):
        r = report(arm, "d10", 1)
        if not r:
            print(f"{arm:<10} (pending)")
            continue
        s = r["summary"]
        d = r["delivery"]
        print(f"{arm:<10} C={s['correct']}/{s['total']} J={s['j_agent_correct_and_within_2']} "
              f"unk={s['unknown']} meanS={round(s['mean_s_agent'],2)} "
              f"bytes~{d['mean_delivered_bytes']:.0f} estTok~{d['mean_estimated_tokens']:.0f} "
              f"req={d['total_actual_requests']}")


def d23x3(arm) -> dict | None:
    reps = [report(arm, "d23", i) for i in (1, 2, 3)]
    if any(r is None for r in reps):
        return None
    cs = [r["summary"]["correct"] for r in reps]
    js = [r["summary"]["j_agent_correct_and_within_2"] for r in reps]
    ss = [r["summary"]["mean_s_agent"] for r in reps]
    return {"arm": arm, "C_by_rep": cs, "J_by_rep": js, "meanS_by_rep": [round(x, 2) for x in ss],
            "mean_C": mean(cs), "mean_J": mean(js), "mean_S": mean(ss)}


def rx3(arm) -> dict | None:
    reps = [report(arm, "r", i) for i in (1, 2, 3)]
    if any(r is None for r in reps):
        return None
    cs = [r["summary"]["correct"] for r in reps]
    js = [r["summary"]["j_agent_correct_and_within_2"] for r in reps]
    ss = [r["summary"]["mean_s_agent"] for r in reps]
    return {"arm": arm, "C_by_rep": cs, "J_by_rep": js, "meanS_by_rep": [round(x, 2) for x in ss],
            "mean_C": mean(cs), "mean_J": mean(js), "mean_S": mean(ss)}


def gate13(arm) -> dict:
    reps = [report(arm, "h2", i) for i in (1, 2, 3)]
    per_q = {q: [] for q in GATE13}
    js = 0
    total = 0
    correct = 0
    for r in reps:
        for case in r["cases"]:
            if case["qid"] in per_q:
                ok = bool(case["correct"])
                inb = bool(case["in_budget"])
                per_q[case["qid"]].append(ok)
                total += 1
                correct += int(ok)
                js += int(ok and inb)
    return {"correct": correct, "of": total, "accuracy": round(correct / total, 3) if total else None,
            "j": js, "j_rate": round(js / total, 3) if total else None,
            "per_question_correct_of_3": {q: sum(v) for q, v in per_q.items()},
            "reps_loaded": sum(1 for r in reps if r)}


def paired_ci_vs_p2(champ_per_q: dict) -> dict:
    p2 = json.loads((R3 / "p2-h2-report-frozen.json").read_text(encoding="utf-8"))
    p2_per_q = {}
    for qid in GATE13:
        runs = p2["per_question"]["CHAMP"][qid]
        p2_per_q[qid] = sum(1 for k in ("h1", "h2", "h3") if runs[k]["correct"])
    diffs = [champ_per_q[q] - p2_per_q[q] for q in GATE13]
    n = len(diffs)
    md = sum(diffs) / n
    if n > 1 and sum((d - md) ** 2 for d in diffs) > 0:
        sd = math.sqrt(sum((d - md) ** 2 for d in diffs) / (n - 1))
        se = sd / math.sqrt(n)
        t = 2.179  # t(12, 0.975)
        lo, hi = md - t * se, md + t * se
    else:
        lo = hi = md
    return {"mean_diff_per39": round(md * 3, 3), "ci95": [round(lo * 3, 3), round(hi * 3, 3)],
            "per_question_diffs": diffs, "p2_per_q": p2_per_q}


def freeze(candidates: list[str]) -> None:
    print("== D23x3 selection ==")
    rows = []
    for arm in candidates:
        d = d23x3(arm)
        if d:
            rows.append(d)
            print(f"{arm:<10} meanC={d['mean_C']:.2f} meanJ={d['mean_J']:.2f} meanS={d['mean_S']:.2f} "
                  f"C={d['C_by_rep']} J={d['J_by_rep']}")
    rows.sort(key=lambda r: (-r["mean_C"], -r["mean_J"], r["mean_S"]))
    champ = rows[0]["arm"]
    stage_d23 = rows[0]["mean_J"] >= 17
    print(f"champion by prereg rule: {champ} (D23x3 stage line J>=17: {'PASS' if stage_d23 else 'FAIL'})")
    manifest = {
        "freeze_id": "POWE-3-R3-CHAMPION-1",
        "champion": champ,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provenance": "frozen AFTER D23x3 selection, BEFORE any R or H2 run of this round (POWE-152)",
        "selection_rule": "preregistered: D23x3 mean C desc -> mean J desc -> mean S asc",
        "d23x3": rows,
        "stage_line_d23": "PASS" if stage_d23 else "FAIL",
        "git_head": subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                   capture_output=True, text=True).stdout.strip(),
    }
    # champion artifact digests
    import hashlib

    def sha_dir(p):
        d = hashlib.sha256()
        for f in sorted(x for x in Path(p).rglob("*") if x.is_file()):
            d.update(f.relative_to(p).as_posix().encode())
            d.update(f.read_bytes())
        return d.hexdigest()

    common = {"p2": WORK / "artifacts/p2-additive-g1.txt", "s3v1": R3 / "commons/s3v1.txt",
              "s3v2": R3 / "commons/s3v2.txt", "s2render": R3 / "commons/s2render.txt",
              "s2direct": R3 / "commons/s2direct.txt"}[champ]
    manifest["artifacts"] = {
        "skills_root_sha256": sha_dir(WORK / "artifacts/p2-additive"),
        "common_sha256": hashlib.sha256(Path(common).read_bytes()).hexdigest(),
        "common_path": str(common),
    }
    (R3 / "freeze" / "champion-r3.json").write_text(json.dumps(manifest, indent=1))
    with (R3 / "lineage.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": manifest["frozen_at"], "event": "champion_frozen", **manifest}) + "\n")
    print("freeze manifest:", R3 / "freeze" / "champion-r3.json")


def final(arm: str) -> None:
    print(f"== final regressions: {arm} ==")
    r = rx3(arm)
    if r:
        print(f"R x3:      C={r['C_by_rep']} J={r['J_by_rep']} meanJ={r['mean_J']:.2f} "
              f"(stage >=14: {'PASS' if r['mean_J'] >= 14 else 'FAIL'}; p2 frozen 18.0)")
    g = gate13(arm)
    print(f"H2 gate13: correct={g['correct']}/{g['of']} acc={g['accuracy']} j={g['j']} j_rate={g['j_rate']} "
          f"(p2 frozen 25/39=64.1%, j=18)")
    paired = paired_ci_vs_p2(g["per_question_correct_of_3"])
    print(f"paired vs p2 (per39): mean {paired['mean_diff_per39']:+.3f} CI95 {paired['ci95']}")
    d = d23x3(arm)
    if d:
        print(f"D23x3:     meanC={d['mean_C']:.2f} meanJ={d['mean_J']:.2f}")
    verdict = {
        "h2_gate13_beats_p2": g["correct"] > P2_H2_FROZEN_GATE["correct"],
        "rx3_stage_line": (r["mean_J"] >= 14) if r else None,
        "d23x3_c_no_regression": (d["mean_C"] >= P2_D23_SINGLE["c"] - 1) if d else None,
        "paired_ci": paired,
    }
    verdict["push_branch"] = bool(verdict["h2_gate13_beats_p2"] and verdict["rx3_stage_line"]
                                  and verdict["d23x3_c_no_regression"])
    print("better-result verdict:", json.dumps({k: v for k, v in verdict.items() if k != "paired_ci"}))
    out = {"arm": arm, "rx3": r, "gate13": g, "d23x3": d, "verdict": verdict,
           "p2_frozen": {"gate13": P2_H2_FROZEN_GATE, "r_mean_j": P2_R_FROZEN, "d23_single": P2_D23_SINGLE}}
    (R3 / "reports" / f"final-{arm}.json").write_text(json.dumps(out, indent=1, default=str))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["screen", "freeze", "final"])
    p.add_argument("--candidates", default="p2,s3v1,s3v2")
    p.add_argument("--arm", default=None)
    args = p.parse_args()
    if args.cmd == "screen":
        screen()
    elif args.cmd == "freeze":
        freeze([c for c in args.candidates.split(",") if c])
    elif args.cmd == "final":
        final(args.arm)


if __name__ == "__main__":
    main()
