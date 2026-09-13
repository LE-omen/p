# POWE-146 iteration records (B/C continuation, relaxed boundary)

Portfolio round: D2 coverage kept via c7's required-skill cards (d2-patterns +
negative-guards [+ legacy-patterns in the champion]) while old-R discipline is
restored by POWE-138 champion C4's structured-template knowledge file
(sha256 08a8766330d8...) delivered through external_knowledge.

Champion: p2-additive — D2 D23 C=23/23 J=19; old R×3 J_agent 19/18/17 (mean
18.0 >= 14/23 stage line). Freeze POWE-3-D6-ITER-CHAMPION-1 landed after D23
and before any R run. No datus-agent code was modified this round (boundary
relaxation not needed; portfolio uses native fields only).

- bc_drive.py — driver (adapted from POWE-145; adds --inject for portfolio
  packaging and a `cost` subcommand reading token_usage records)
- candidates.json / lineage.jsonl — candidate registry and event lineage
- champion-freeze.json — freeze proof (digests, provenance, selection rule)
- c4-templates-knowledge.txt — C4 appendix byte-exact ingredient (base +
  "\n\n" + this file == common-c4.txt)
- reports/*.json — screening and R scores (unified scorer, eval HEAD d88f2a4b)
- cost-table.json / r3-summary.json / r-per-question.json — cost and outcomes

Reproduction: branch swift/powe-146-iter (local); harness per bc_drive.py;
runtime = pinned datus 0.4.0 (b38e71c3) venv from the POWE-132 workspace;
model deepseek-v4-flash-0731 temp 0; secrets from environment, never persisted.
