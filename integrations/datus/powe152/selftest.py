# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""POWE-152 S1/S2 self-test (no model calls).

Checks, in order:

1. S1 static consistency — the p2-additive champion artifacts rebuild the
   frozen digests (aggregate 560a30eb..., common 08a87663...), and the
   contract verifies against the materialized files.
2. S1 off-switch equivalence — with ``POWERCONTEXT_DELIVERY=off`` the ledger
   changes no delivered bytes and stays observational.
3. S1 replay — one executed run directory replays into a ledger with full
   per-channel accounting.
4. S2 offline negative suite — every unsafe case is rejected; valid cases
   render single read-only SELECTs.
5. S2 store projection — frozen templates project into an isolated datus
   reference_template store and read back exactly.
6. S3 guard structure — every experience entry carries all mandatory guard
   fields; manifests freeze with digests.

Usage (inside the datus runtime venv, or any python with jinja2 available):
    python selftest.py WORKDIR [--smoke-qdir PATH]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from powercontext_datus import delivery_ledger as dl  # noqa: E402
from powercontext_datus import experience_entries as ee  # noqa: E402
from powercontext_datus import s2templates as s2  # noqa: E402

FROZEN = {
    "skills_root": "560a30eb2f48d1f2aa983099ac8456c5009145e4feee3aa93f08c4076d347f30",
    "common": "08a8766330d89d832fd3d34a7a93e6d44643a2b3a868a2e4640baee0c91268ac",
}

results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:300]})
    print(("PASS" if ok else "FAIL"), name, ("— " + detail[:160]) if detail else "")


def main() -> int:
    workdir = Path(sys.argv[1]).resolve()
    artifacts = workdir / "artifacts"
    p2_skills = artifacts / "p2-additive"
    p2_common = artifacts / "p2-additive-g1.txt"

    # 1. S1 static consistency vs frozen champion digests.
    check("s1.skills_root_matches_frozen_champion", dl.sha256_dir(p2_skills) == FROZEN["skills_root"])
    check("s1.common_matches_frozen_champion", dl.sha256_file(p2_common) == FROZEN["common"])
    contract = dl.contract_from_condition(
        label="p2-additive", skills_dir=p2_skills,
        skill_names=["d2-patterns", "legacy-patterns", "negative-guards"],
        common_path=p2_common,
        scope={"datasource": "birdbench", "dialect": "mysql",
               "schema_fingerprint": "birdbench-debit_card_specializing-v2"},
    )
    v = dl.verify_contract(contract, skills_dir=p2_skills, common_path=p2_common)
    check("s1.contract_verifies_materialized", v["all_ok"], json.dumps(v["checks"]))
    rebuild = dl.contract_from_condition(
        label="p2-additive", skills_dir=p2_skills,
        skill_names=["d2-patterns", "legacy-patterns", "negative-guards"],
        common_path=p2_common,
        scope={"datasource": "birdbench", "dialect": "mysql",
               "schema_fingerprint": "birdbench-debit_card_specializing-v2"},
    )
    same = rebuild["bundle_id"] == contract["bundle_id"]
    check("s1.contract_replay_deterministic", same, f"bundle_id={contract['bundle_id'][:16]}")

    # 2. S1 off-switch equivalence on a synthetic ledger replay.
    smoke = None
    for arg in sys.argv[2:]:
        if arg.startswith("--smoke-qdir="):
            smoke = Path(arg.split("=", 1)[1])
    if smoke and (smoke / "result.json").exists():
        os.environ["POWERCONTEXT_DELIVERY"] = "off"
        ledger_off = dl.account_run(smoke, contract=contract, plan_common_path=p2_common)
        os.environ["POWERCONTEXT_DELIVERY"] = "on"
        ledger_on = dl.account_run(smoke, contract=contract, plan_common_path=p2_common)
        bytes_equal = ledger_off["budget"]["total_bytes"] == ledger_on["budget"]["total_bytes"]
        check("s1.offswitch_preserves_delivered_bytes", bytes_equal,
              f"off={ledger_off['budget']['total_bytes']} on={ledger_on['budget']['total_bytes']}")
        check("s1.offswitch_ledger_still_emitted", ledger_off["delivery_disabled"] is True
              and ledger_off["channels"]["prompt"]["bytes"] > 0)
        check("s1.replay_accounts_all_channels",
              all(c in ledger_on["channels"] for c in ("prompt", "tools", "external_knowledge")))
        check("s1.replay_reports_full_delivery", ledger_on["delivery_mode_reported"] == "full_delivery")
        # The smoke run is the N arm (no injected skills); the p2 contract must
        # flag every contracted-but-not-delivered skill card — misdelivery
        # detection working, not a defect.
        detected = {m["artifact_id"] for m in ledger_on["misdeliveries"]}
        check("s1.misdelivery_detection_flags_mismatch",
              detected == {"d2-patterns", "legacy-patterns", "negative-guards"},
              json.dumps(ledger_on["misdeliveries"])[:200])

    # 3. S2 offline negative suite.
    cases = s2.offline_negative_cases()
    bad = [c for c in cases if not c["ok"]]
    check("s2.negative_suite_all_expected", not bad, json.dumps(bad)[:200])
    inj = next(c for c in cases if c["case"].endswith("neutralized"))
    check("s2.injection_neutralized_by_escaping", "'KAM'' OR ''1''=''1'" in inj["sql"],
          inj["sql"][:120])
    # Rendered valid SQL shape checks.
    entries = {e["name"]: e for e in s2.build_template_set()}
    t02_sql = s2.render_template(entries["t02-cust-ym-consumption-extremum"],
                                 {"selector": "CustomerID", "direction": "max", "period": "month",
                                  "month": "201208", "with_total": False})
    check("s2.t02_valid_render_is_single_select",
          t02_sql.upper().startswith("SELECT") and ";" not in t02_sql, t02_sql[:120])
    t01_sql = s2.render_template(entries["t01-dim-filter-count"],
                                 {"table": "customers", "col1": "Segment", "v1": "KAM",
                                  "col2": "Currency", "v2": "EUR"})
    check("s2.t01_two_filter_render", "Segment = 'KAM'" in t01_sql and "Currency = 'EUR'" in t01_sql, t01_sql[:120])
    quote_sql = s2.render_template(entries["t01-dim-filter-count"],
                                   {"table": "customers", "col1": "Segment", "v1": "O'Brien"})
    check("s2.quote_escaping_doubled", "'O''Brien'" in quote_sql, quote_sql[:120])

    # 4. S2 freeze + store projection round-trip (needs datus runtime).
    freeze = s2.freeze_template_set(list(entries.values()))
    check("s2.freeze_manifest_has_digests", bool(freeze["set_sha256"]) and len(freeze["templates"]) == 12,
          f"set={freeze['set_sha256'][:16]} n={len(freeze['templates'])}")
    try:
        import jinja2  # noqa: F401
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home" / ".datus"
            proj = s2.project_store(home, list(entries.values()))
            check("s2.store_projection_roundtrip", proj["store_size"] == 12, json.dumps(proj))
    except Exception as e:  # pragma: no cover - outside datus runtime
        check("s2.store_projection_roundtrip", False, f"runtime missing: {e}")

    # 5. S3 guard structure completeness.
    mandatory = ["applicability", "scope", "required_tools", "procedure", "output_granularity",
                 "null_empty_semantics", "mismatch_fallback", "guards", "source"]
    for variant, builder in (("v1", ee.build_v1_entries), ("v2", ee.build_v2_entries)):
        entries_s3 = builder()
        complete = all(all(e.get(k) for k in mandatory) for e in entries_s3)
        check(f"s3.{variant}_guard_structure_complete", complete)
        manifest = ee.freeze_experience(entries_s3, variant=variant)
        check(f"s3.{variant}_manifest_frozen", bool(manifest["set_sha256"]))

    ok_all = all(r["ok"] for r in results)
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} checks passed")
    out = Path(__file__).resolve().parent / "selftest-results.json"
    out.write_text(json.dumps({"ok": ok_all, "results": results,
                               "freeze": freeze}, ensure_ascii=False, indent=1))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
