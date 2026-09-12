# POWE-137 ARM-B: failure-feedback reflection iteration (DESIGN-5 three-arm trial)

Driver scripts for arm B. Runs against the frozen DESIGN-5 baseline
(`f7ab4768`, POWE-132 shared prerequisites) on branch `swift/powe-137-arm-b`
(local only, never pushed).

Mechanism (DESIGN-5 finalization `01a094d8`):

1. Gen1: three hypothesis-distinct knowledge cards derived from the frozen E1
   evidence pool (server-side generation attempted first; outputs were rejected
   at admission review as provenance-echo and archived in the run workspace).
   Cards are delivered by direct injection (`run_qa.py --batch`, required-skill).
2. Reflection: every generation is evaluated on the D screening slices
   (`d5` = screen_5, `d10` = screen_10, `d23` = full D) with the frozen unified
   scorer; failed/unknown cases produce the evaluator feedback digest the
   generator is allowed to read (`score_runs.py --set d*` writes `feedback`).
3. Revisions cite that feedback in `records/candidates.json` lineage
   (revision_basis); merges of complementary candidates must retest.
4. Champion is frozen (`freeze_champion.py`: tree digest + SKILL.md sha256 +
   UTC timestamp + selection evidence) strictly BEFORE any old-R run starts.
5. Old R (zero-based odd CSV qids) is then run 3x under the identical unified
   scorer; expected rows come from `build_r_oracle.py` (read-only, SELECT-only
   independent execution of the authorized judgment CSV, same treatment E1 gave
   the learning half).

Scoring note: recorded DECIMAL cells arrive as the bridge transport encoding
`{"decimal": "..."}` (`capture.encode_cell`); `score_runs.py` decodes them with
the frozen `e1_build.normalize_cell` policy (Decimal -> float) before the frozen
comparator, mirroring how E1 expected rows were built.

Reproduction (from the repo root, datus runtime env for `run_qa.py`):

```
# 1. D evaluation of one candidate (card lives in the run workspace)
python integrations/datus/armb/make_plans.py --cand c6-semantics-check --set d10 \
    --skill-name b6-sql-discipline-v2 --out plan.json
cd integrations/datus/runtime && set -a; . <keys env>; set +a
uv run --frozen python ../../armb/run_qa.py --batch <abs path to plan.json>

# 2. Unified scoring + failure feedback
PYTHONPATH=integrations/datus/src:. uv run --frozen python integrations/datus/armb/score_runs.py \
    --cand c6-semantics-check --set d10 --out score.json

# 3. Old-R oracle (evaluator side; read-only) and R runs
DB_PASSWORD=... python integrations/datus/armb/build_r_oracle.py --csv <judgment csv> --out r-oracle.json
# ... make_plans.py --set r1/r2/r3, run_qa.py batches, score_runs.py --set rN

# 4. Champion freeze before R; cost aggregation
PYTHONPATH=... python integrations/datus/armb/freeze_champion.py --cand ... --skill-name ... \
    --selection-evidence ... --out freeze.json
python integrations/datus/armb/cost_table.py
```

Secrets stay in the run environment (`/home/rongfeng.frf/workspace/.env` and the
workspace-local `.env.db`); nothing here reads or stores credentials.
