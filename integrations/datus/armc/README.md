# POWE-138 ARM-C artifacts (DESIGN-5 C arm)

Branch `swift/powe-138-arm-c`, base = frozen HEAD `f7ab476864eb92ab1ea8efe3c155027d1845a3fe`.
Not pushed (per user constraint).

- `freeze-proof.json` — champion frozen from D5+D10 screening BEFORE any old-R run (commit on this branch, timestamped).
- `artifacts/descriptors-v1.json` — 8 structured descriptors (params / applicability / structure / SQL template / output contract / evidence), synthesized deterministically from frozen E1 (REFERENCE_SQL x23 + native x8; negative x12 -> guards).
- `artifacts/guard-evidence.json` — mechanical negative-vs-reference structural diffs (12 rows).
- `artifacts/validation-report.json` — 54/54 structure + counterexample checks on isolated in-memory synthetic data (duplicates/NULL/empty/grain/rounding/time-boundary/scope-denominator/semantic-binding/direction/parameterization).
- `artifacts/db-spotcheck.json` — 5 read-only SELECT parameterization spot checks.
- `artifacts/candidate-registry.json`, `screening-d5.json`, `screening-d10.json` — 4 renderings (C1 skill-compact, C2 skill+worked-SQL, C3 three split skills, C4 knowledge-file append), D5 4x5, D10 2x10, shared selector J->C->meanS->unknown.
- `artifacts/champion-C4-common.txt` — the frozen champion artifact (content digest 08a8766330d8..., exact bytes injected as external_knowledge).
- `artifacts/r3-report.json`, `fallback-analysis.json`, `cost-summary.json`, `ledger-r*.json` — old-R x3 results, template-hit vs native-fallback accounting, token/SQL cost.
- `artifacts/skills/` — rendered SKILL.md for C1/C2/C3.
- `scripts/` — reproduction drivers (runner `run_qa.py` reuses the reviewed bridge components per the user's 2026-09-11 authorization; same invocation as POWE-132).

Reproduce: see scripts; python3 scripts/synthesize_descriptors.py -> validate_descriptors.py -> render_candidates.py; run d5/d10 via drive_screen.py; freeze+R x3 via run_r3.py auto. Model deepseek-v4-flash-0731 (dashscope), read-only birdbench; secrets via env (OPENAI_API_KEY, DB_PASSWORD), never committed.
