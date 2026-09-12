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

"""Swift-mode self-extraction driver for POWE-131.

Implements steps 2-3 of the four-step chain on top of the reviewed bridge:

2. Developer-selected native run traces are converted mechanically into
   LearningEvidence and captured as content sources (the only human-judgment
   step; every selected trace must still pass the mechanical verification in
   powercontext_datus.trace_learning).
3. PowerContext itself extracts managed Skills from the captured evidence
   (server-side LLMSkillGenerator), the pending candidates go through the
   approval chain, and the approved packages are delivered into a Datus
   skill root with exact digest verification.

No Skill content is authored here. Runs in the main repository environment:

  uv run --frozen python integrations/datus/swift_selfextract.py \
      --selection sel.json --skill-root DIR --audit audit.json --db pcx.sqlite

Model credentials come from the environment (OPENAI_API_KEY, plus
POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL / OPENAI_BASE_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "datus" / "src"))

import httpx  # noqa: E402

from powercontext.builtin.persistence.sqlite import SQLiteConfig  # noqa: E402
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime  # noqa: E402
from powercontext.builtin.runtime.config import InferenceConfig  # noqa: E402
from powercontext.client import PowerContextClient  # noqa: E402
from powercontext.http import (  # noqa: E402
    ApproveArtifactCandidateRequest,
    GetArtifactCandidateRequest,
)
from powercontext.server.app import create_app  # noqa: E402

from powercontext_datus.delivery import deliver_skill  # noqa: E402
from powercontext_datus.learning import generate_from_examples  # noqa: E402
from powercontext_datus.trace_learning import evidence_from_run, tables_of  # noqa: E402


def _load_run(run_dir: Path) -> tuple[dict, list[dict], dict]:
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads((run_dir / "payload.json").read_text(encoding="utf-8"))
    return result, records, manifest


async def _scenario(options: argparse.Namespace) -> dict:
    selection = json.loads(Path(options.selection).read_text(encoding="utf-8"))
    oracle = json.loads(Path(options.oracle).read_text(encoding="utf-8"))
    runs_root = Path(selection["runs_root"])

    evidences = []
    for item in selection["items"]:
        run_dir = runs_root / f"q{item['qid']}"
        result, records, manifest = _load_run(run_dir)
        sql_entry = None
        if item.get("kind") == "intermediate":
            from powercontext_datus.trace_learning import matching_executions

            hits = matching_executions(result, (oracle[str(item["qid"])].get("expected") or {}).get("rows") or [])
            if not hits:
                raise SystemExit(f"selection q{item['qid']} has no oracle-matching executed SQL")
            sql_entry = hits[0]
        evidence = evidence_from_run(
            qid=item["qid"],
            result=result,
            records=records,
            oracle_case=oracle[str(item["qid"])],
            manifest=manifest,
            sql_entry=sql_entry,
            final_verified=item.get("kind", "final") == "final",
        )
        if evidence is None:
            raise SystemExit(f"selection q{item['qid']} failed mechanical verification; refusing to inject")
        evidences.append(evidence)

    groups: dict[frozenset[str], list] = {}
    for evidence in evidences:
        groups.setdefault(tables_of(evidence.sql), []).append(evidence)

    inference = InferenceConfig(
        generation_model=os.environ["POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL"],
        generation_base_url=os.environ.get("POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL")
        or os.environ["OPENAI_BASE_URL"],
        generation_timeout_seconds=float(os.environ.get("SWIFT_GENERATION_TIMEOUT_S", "180")),
        generation_max_requests=int(os.environ.get("SWIFT_GENERATION_MAX_REQUESTS", "3")),
    )
    database = SQLiteConfig(url=f"sqlite+aiosqlite:///{Path(options.db).resolve()}")
    skill_root = Path(options.skill_root)
    skill_root.mkdir(parents=True, exist_ok=True)

    audit: dict[str, object] = {
        "selection_criteria": selection.get("criteria", []),
        "injected_evidence": [
            {"sample_id": e.sample_id, "question": e.question, "sql": e.sql, "lesson": e.lesson}
            for e in evidences
        ],
        "extraction_groups": [],
        "skills_installed": [],
    }

    async with open_builtin_runtime(BuiltinConfig(database=database, inference=inference)) as runtime:
        app = create_app(application=cast("ServerApplication", runtime))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as transport:
            client = PowerContextClient("http://testserver", http_client=transport, trust_transport_security=True)
            scope_id = (await client.get_default_scope()).scope_id
            audit["scope_id"] = scope_id

            for key in sorted(groups, key=lambda t: sorted(t)):
                group = groups[key]
                group_record: dict[str, object] = {
                    "tables": sorted(key),
                    "samples": [e.sample_id for e in group],
                }
                audit["extraction_groups"].append(group_record)
                generated = await generate_from_examples(client, scope_id=scope_id, examples=group)
                group_record["generated"] = generated.candidate is not None
                if generated.candidate is None:
                    group_record["note"] = "server returned an explicit no-op for this evidence"
                    continue
                candidate = generated.candidate
                if candidate is None:  # pragma: no cover - guarded by generated flag
                    raise SystemExit("generated flag without candidate")
                group_record["candidate"] = {
                    "candidate_id": candidate.candidate_id,
                    "version": candidate.version,
                    "status": candidate.status.value,
                    "family": candidate.family.value,
                    "source_refs": [ref.model_dump(mode="json") for ref in candidate.source_refs],
                    "proposal": candidate.proposal.model_dump(mode="json"),
                }
                approved = await client.approve_artifact_candidate(
                    ApproveArtifactCandidateRequest(
                        scope_id=scope_id,
                        candidate_id=candidate.candidate_id,
                        expected_version=candidate.version,
                    )
                )
                if approved.result_artifact is None:
                    raise SystemExit(f"approval produced no artifact for candidate {candidate.candidate_id}")
                after = await client.get_artifact_candidate(
                    GetArtifactCandidateRequest(scope_id=scope_id, candidate_id=candidate.candidate_id)
                )
                group_record["approval"] = {
                    "status_after": approved.status.value,
                    "candidate_status_after": after.status.value,
                    "result_artifact": approved.result_artifact.model_dump(mode="json"),
                }
                installed = await deliver_skill(
                    client, scope_id=scope_id, artifact=approved.result_artifact, skill_root=skill_root
                )
                installed.pop("files", None)
                group_record["delivery"] = installed
                audit["skills_installed"].append(
                    {
                        "name": installed["name"],
                        "artifact": approved.result_artifact.model_dump(mode="json"),
                        "tree_digest": installed["tree_digest"],
                        "archive_digest": installed["archive_digest"],
                    }
                )
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, help="selection JSON (criteria + items)")
    parser.add_argument("--oracle", required=True, help="oracle JSON keyed by question id")
    parser.add_argument("--skill-root", required=True, help="destination root for delivered skill packages")
    parser.add_argument("--audit", required=True, help="audit JSON output path")
    parser.add_argument("--db", required=True, help="sqlite database file for the runtime")
    parser.add_argument("--env-file", default="/home/rongfeng.frf/workspace/.env")
    options = parser.parse_args()
    load_dotenv(options.env_file)
    audit = asyncio.run(_scenario(options))
    Path(options.audit).write_text(json.dumps(audit, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(
        "self-extraction complete:",
        f"evidence={len(audit['injected_evidence'])}",
        f"groups={len(audit['extraction_groups'])}",
        f"skills={len(audit['skills_installed'])}",
    )


if __name__ == "__main__":
    main()
