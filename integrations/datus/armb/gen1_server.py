"""POWE-137 ARM-B Gen-1: server-side candidate generation from E1 hypothesis slices.

Reuses the reviewed POWE-131/132 chain (learning.generate_from_examples ->
server LLMSkillGenerator -> approve -> deliver_skill with digest verification),
feeding LearningEvidence reconstructed from the frozen E1 manifest entries
(origin REFERENCE_SQL / native only; negative and unverified never enter
generation). Three hypothesis-distinct evidence slices produce three
candidates for direct-injection delivery.

Runs in the main repository environment:
  PYTHONPATH=integrations/datus/src uv run --frozen python gen1_server.py \
      --manifest integrations/datus/e1/manifest.json --out-root <armb>
Secrets: OPENAI_API_KEY / OPENAI_BASE_URL / generation model env from
/home/rongfeng.frf/workspace/.env (never printed, never committed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

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
from powercontext_datus.learning import LearningEvidence, generate_from_examples  # noqa: E402
from powercontext_datus.freeze import snapshot  # noqa: E402

# Three hypothesis slices over the frozen E1 evidence pool (learning half only).
HYPOTHESES = {
    "B1-agg-ratio": {
        "qids": [0, 6, 8, 12, 14, 16, 18, 20, 44, 4],
        "hypothesis": (
            "Conditioned aggregation idioms transfer: SUM(IF(cond,1,0)) differences, "
            "CAST(... AS FLOAT) ratio shares, SUBSTR year/month windows, GROUP BY over "
            "joined tables with ORDER BY+LIMIT extremes."
        ),
    },
    "B2-join-dims": {
        "qids": [24, 26, 28, 30, 34, 36, 38, 40, 42],
        "hypothesis": (
            "Explicit join paths across customers/gasstations/products/transactions_1k/"
            "yearmonth and exact predicate conventions (Date='YYYY-MM-DD', Time='HH:MM:SS', "
            "binary-cased Segment/Country/Currency codes, DISTINCT lists) transfer."
        ),
    },
    "B3-extremes": {
        "qids": [2, 4, 8, 10, 16, 22, 32, 42],
        "hypothesis": (
            "Argmin/argmax patterns transfer: GROUP BY entity + ORDER BY aggregate "
            "ASC/DESC LIMIT 1, monthly windows via SUBSTR(Date,5,2), paired-extreme "
            "questions returning (id, value)."
        ),
    },
}


def evidence_from_entry(entry: dict) -> LearningEvidence:
    return LearningEvidence(
        sample_id=entry["sample_id"],
        question=entry["question"],
        sql=entry["sql"],
        result_digest=entry["result_digest"],
        native_receipt_digest=entry["native_receipt_digest"],
        source_manifest_digest=entry["source_manifest_digest"],
        lesson=entry["lesson"],
    )


async def run(options: argparse.Namespace) -> dict:
    manifest = json.loads(Path(options.manifest).read_text(encoding="utf-8"))
    by_qid: dict[int, dict] = {}
    for entry in manifest["entries"]:
        if entry["origin"] not in ("REFERENCE_SQL", "native"):
            continue
        by_qid.setdefault(entry["qid"], entry)  # prefer REFERENCE_SQL representation

    out_root = Path(options.out_root)
    db_file = out_root / "records" / "gen1-server.sqlite"
    audit: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "e1_manifest_sha256": manifest.get("digest", ""),
        "candidates": [],
    }

    inference = InferenceConfig(
        generation_model=__import__("os").environ["POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL"],
        generation_base_url=__import__("os").environ.get(
            "POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL"
        ) or __import__("os").environ["OPENAI_BASE_URL"],
        generation_timeout_seconds=float(__import__("os").environ.get("SWIFT_GENERATION_TIMEOUT_S", "180")),
        generation_max_requests=int(__import__("os").environ.get("SWIFT_GENERATION_MAX_REQUESTS", "3")),
    )
    database = SQLiteConfig(url=f"sqlite+aiosqlite:///{db_file.resolve()}")
    async with open_builtin_runtime(BuiltinConfig(database=database, inference=inference)) as runtime:
        app = create_app(application=runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as transport:
            client = PowerContextClient("http://testserver", http_client=transport, trust_transport_security=True)
            scope_id = (await client.get_default_scope()).scope_id
            audit["scope_id"] = scope_id

            for cand_id, spec in HYPOTHESES.items():
                examples = [evidence_from_entry(by_qid[q]) for q in spec["qids"]]
                skill_root = out_root / "skills" / cand_id
                skill_root.mkdir(parents=True, exist_ok=True)
                record: dict = {
                    "candidate_id": cand_id,
                    "hypothesis": spec["hypothesis"],
                    "evidence_samples": [e.sample_id for e in examples],
                    "evidence_qids": spec["qids"],
                }
                generated = await generate_from_examples(client, scope_id=scope_id, examples=examples)
                record["server_generated"] = generated.candidate is not None
                if generated.candidate is None:
                    record["note"] = "server returned an explicit no-op for this evidence"
                    audit["candidates"].append(record)
                    continue
                candidate = generated.candidate
                record["server_candidate"] = {
                    "candidate_id": candidate.candidate_id,
                    "version": candidate.version,
                    "family": candidate.family.value,
                }
                approved = await client.approve_artifact_candidate(
                    ApproveArtifactCandidateRequest(
                        scope_id=scope_id,
                        candidate_id=candidate.candidate_id,
                        expected_version=candidate.version,
                    )
                )
                if approved.result_artifact is None:
                    record["note"] = "approval produced no artifact"
                    audit["candidates"].append(record)
                    continue
                installed = await deliver_skill(
                    client, scope_id=scope_id, artifact=approved.result_artifact, skill_root=skill_root
                )
                installed.pop("files", None)
                record["delivery"] = installed
                record["skill_name"] = installed["name"]
                record["tree_digest"] = installed["tree_digest"]
                record["files"] = snapshot(skill_root / str(installed["name"]))
                audit["candidates"].append(record)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--audit-out", required=True)
    parser.add_argument("--env-file", default="/home/rongfeng.frf/workspace/.env")
    options = parser.parse_args()
    load_dotenv(options.env_file)
    audit = asyncio.run(run(options))
    Path(options.audit_out).write_text(json.dumps(audit, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    made = [c["candidate_id"] for c in audit["candidates"] if c.get("tree_digest")]
    print(f"gen1 server generation complete: candidates={made}")


if __name__ == "__main__":
    main()
