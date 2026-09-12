"""POWE-137 ARM-B: aggregate per-candidate cost and per-question ledger detail.

Walks runs/<cand>/<set>/q*/result.json, summing model requests, HTTP attempts and
tokens from the frozen S_agent step ledger, plus wall-clock run time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    rows = []
    for cand_dir in sorted((root / "runs").iterdir()):
        if not cand_dir.is_dir():
            continue
        for set_dir in sorted(cand_dir.iterdir()):
            if not set_dir.is_dir():
                continue
            agg = {"cand": cand_dir.name, "set": set_dir.name, "runs": 0, "model_requests": 0,
                   "http_attempts": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                   "s_agent_sum": 0, "s_agent_known": 0, "wall_s": 0.0}
            for qdir in sorted(set_dir.iterdir()):
                rf = qdir / "result.json"
                if not rf.exists():
                    continue
                r = json.loads(rf.read_text(encoding="utf-8"))
                agg["runs"] += 1
                led = r.get("step_ledger") or {}
                agg["model_requests"] += led.get("model_requests") or 0
                agg["http_attempts"] += led.get("http_attempts") or led.get("model_http_attempts") or 0
                for k in ("input_tokens", "tokens_input"):
                    agg["tokens_in"] += led.get(k) or 0
                for k in ("output_tokens", "tokens_output"):
                    agg["tokens_out"] += led.get(k) or 0
                for k in ("total_tokens", "tokens_total"):
                    agg["tokens_total"] += led.get(k) or 0
                s = r.get("s_agent")
                if isinstance(s, int):
                    agg["s_agent_sum"] += s
                    agg["s_agent_known"] += 1
                if r.get("started_at") and r.get("finished_at"):
                    agg["wall_s"] += float(r["finished_at"]) - float(r["started_at"])
            if agg["tokens_total"] == 0:
                agg["tokens_total"] = agg["tokens_in"] + agg["tokens_out"]
            rows.append(agg)
    out = root / "records" / "cost-table.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for a in rows:
        print(f"{a['cand']}/{a['set']}: runs={a['runs']} req={a['model_requests']} "
              f"tok={a['tokens_total']} meanS={a['s_agent_sum']/a['s_agent_known'] if a['s_agent_known'] else None:.2f} "
              if a['s_agent_known'] else
              f"{a['cand']}/{a['set']}: runs={a['runs']} req={a['model_requests']} tok={a['tokens_total']} meanS=None")


if __name__ == "__main__":
    sys.exit(main())
