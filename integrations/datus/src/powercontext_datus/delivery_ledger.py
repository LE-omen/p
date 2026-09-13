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

"""S1 delivery contract and per-run delivery accounting (POWE-152, DESIGN-7 P0).

A *delivery bundle* is the versioned manifest of every artifact the harness
hands to datus for one condition: which artifacts, at which digests, through
which channel, under which applicability, from which source.  The *per-run
ledger* replays one executed question directory (trace.jsonl + result.json)
against its bundle and reports what was actually delivered: bytes and
estimated tokens per channel, real model usage, failures, budget state, and
any misdelivery (artifact loaded at runtime that the contract does not cover,
or a contract artifact whose materialized digest drifted).

Discipline implemented here (DESIGN-7 S1 clauses):

* full delivery is reported as full delivery — never as "recall hit";
* a scope/digest mismatch refuses the delivery (empty increment, native flow
  continues) instead of silently shipping an unknown artifact;
* accounting includes failures and rollbacks, so results are recomputable;
* ``delivery_disabled`` (plan flag / ``POWERCONTEXT_DELIVERY=off``) keeps the
  accounting purely observational and changes no delivered bytes — with the
  flag off the ledger is still emitted for the run, marked observational.

No model calls are made from this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "powercontext.delivery_ledger/1"

# Byte budget defaults per channel (byte control after artifact growth).
DEFAULT_BUDGET = {
    "required_skill_bytes": 120_000,
    "external_knowledge_bytes": 16_000,
    "prompt_bytes": 60_000,
    "tools_bytes": 40_000,
    "total_bytes": 220_000,
}


def sha256_dir(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in Path(path).rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_record(
    *,
    artifact_id: str,
    kind: str,
    revision: str,
    channel: str,
    path: Path | None = None,
    text: str | None = None,
    digest: str | None = None,
    size: int | None = None,
    applicability: str,
    source: str,
) -> dict[str, Any]:
    """Build one contract artifact entry with its content digest.

    ``channel`` is the datus-native delivery surface: ``required_skill``,
    ``external_knowledge``, ``prompt`` or ``tools``.
    """
    if digest is None:
        if path is not None:
            if Path(path).is_dir():
                digest = sha256_dir(Path(path))
                size = sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
            else:
                digest = sha256_file(Path(path))
                size = Path(path).stat().st_size if size is None else size
        elif text is not None:
            digest = sha256_text(text)
            size = len(text.encode("utf-8")) if size is None else size
        else:
            raise ValueError("artifact_record needs path=, text= or digest=")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "revision": revision,
        "digest": digest,
        "bytes": size if size is not None else 0,
        "channel": channel,
        "applicability": applicability,
        "source": source,
    }


def build_contract(
    *,
    scope: dict[str, str],
    artifacts: list[dict[str, Any]],
    budget: dict[str, int] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Assemble the versioned delivery bundle manifest."""
    contract = {
        "schema_version": SCHEMA_VERSION,
        "scope": dict(scope),  # datasource / dialect / schema_fingerprint
        "budget": {**DEFAULT_BUDGET, **(budget or {})},
        "artifacts": artifacts,
        "notes": notes,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    contract["bundle_id"] = hashlib.sha256(
        json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return contract


def contract_from_condition(
    *,
    label: str,
    skills_dir: Path,
    skill_names: list[str],
    common_path: Path,
    scope: dict[str, str],
    sources: dict[str, str] | None = None,
    extra_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the standard contract for one evaluation condition.

    ``extra_artifacts`` carries S2/S3 additions (template store bundle,
    experience entries) so every delivered byte is covered by the contract.
    """
    sources = sources or {}
    arts: list[dict[str, Any]] = [artifact_record(
        artifact_id="__skills_root__", kind="required_skills_root", revision="r1",
        channel="required_skill", path=skills_dir,
        applicability="all questions (host-injected required skills)",
        source=sources.get("skills_root", "frozen champion artifacts"),
    )]
    for name in skill_names:
        sub = Path(skills_dir) / name
        if sub.exists():
            arts.append(artifact_record(
                artifact_id=name, kind="skill_card", revision="r1",
                channel="required_skill", path=sub,
                applicability="all questions (required-skill injection)",
                source=sources.get(name, "frozen champion artifacts"),
            ))
    if Path(common_path).exists():
        arts.append(artifact_record(
            artifact_id="__common__", kind="external_knowledge_file", revision="r1",
            channel="external_knowledge", path=common_path,
            applicability="all questions (schema + knowledge + answer protocol)",
            source=sources.get("common", "frozen champion artifacts"),
        ))
    arts.append(artifact_record(
        artifact_id="__native_tools__", kind="tool_manifest", revision="datus-b38e71c3",
        channel="tools",
        text="describe_table;execute_sql;list_tables;load_skill",
        applicability="frozen native tool profile (workflow.py TOOL_NAMES)",
        source="datus 0.4.0 pinned runtime",
    ))
    arts.extend(extra_artifacts or [])
    return build_contract(scope=scope, artifacts=arts, notes=f"condition {label}")


def verify_contract(
    contract: dict[str, Any], *, skills_dir: Path | None, common_path: Path | None
) -> dict[str, Any]:
    """Static consistency: contract digests vs the materialized artifacts.

    Replay half of the S1 acceptance check — the same inputs must always hash
    to the same contract, and the runtime materialization must match the
    frozen digests before anything is delivered.
    """
    checks: list[dict[str, Any]] = []
    by_id = {a["artifact_id"]: a for a in contract["artifacts"]}
    if skills_dir is not None and "__skills_root__" in by_id:
        actual = sha256_dir(skills_dir)
        checks.append({"check": "skills_root_digest", "expected": by_id["__skills_root__"]["digest"],
                       "actual": actual, "ok": by_id["__skills_root__"]["digest"] == actual})
    if common_path is not None and "__common__" in by_id:
        actual = sha256_file(common_path)
        checks.append({"check": "external_knowledge_digest", "expected": by_id["__common__"]["digest"],
                       "actual": actual, "ok": by_id["__common__"]["digest"] == actual})
    if skills_dir is not None:
        for art in contract["artifacts"]:
            if art["channel"] == "required_skill" and art["artifact_id"] not in {"__skills_root__"}:
                sub = Path(skills_dir) / art["artifact_id"]
                if sub.exists():
                    actual = sha256_dir(sub)
                    checks.append({"check": "skill_card_digest", "artifact_id": art["artifact_id"],
                                   "expected": art["digest"], "actual": actual, "ok": art["digest"] == actual})
    return {"bundle_id": contract["bundle_id"], "checks": checks, "all_ok": all(c["ok"] for c in checks)}


def _token_estimate(n_bytes: int) -> int:
    # Conservative chars-per-token estimate for mixed SQL/prose; recorded as an
    # estimate, never mixed with the model's own usage numbers.
    return max(0, n_bytes + 3) // 4


def delivery_disabled() -> bool:
    """Rollback switch: accounting stays, delivery semantics unchanged."""
    return os.environ.get("POWERCONTEXT_DELIVERY", "").lower() == "off"


def account_run(
    qdir: Path,
    *,
    contract: dict[str, Any] | None,
    plan_common_path: Path | None,
    plan_note: str | None = None,
) -> dict[str, Any]:
    """Replay one executed question directory into a delivery ledger row."""
    qdir = Path(qdir)
    records: list[dict[str, Any]] = []
    trace_path = qdir / "trace.jsonl"
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    result: dict[str, Any] = {}
    result_path = qdir / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))

    disabled = delivery_disabled() or bool(plan_note and "delivery=off" in plan_note)
    effective = next((r for r in records if r.get("kind") == "effective_config"), None)
    usage_rows = [r for r in records if r.get("kind") == "model_finished" and r.get("usage")]
    cumulative: dict[str, Any] = {}
    for r in records:
        action = r.get("action") or {}
        if r.get("kind") == "action_received" and action.get("action_type") == "token_usage":
            cumulative = action.get("output", {}).get("cumulative") or cumulative
    usage_actual = cumulative or (
        {k: sum((u.get("usage") or {}).get(k) or 0 for u in usage_rows)
         for k in ("requests", "input_tokens", "output_tokens")} if usage_rows else {}
    )

    channels: dict[str, Any] = {}
    skills_snapshot = (effective or {}).get("skills") or {}
    skills_bytes = 0
    loaded_ids: list[str] = []
    for name, entry in sorted(skills_snapshot.items()):
        body = entry.get("content") if isinstance(entry, dict) else None
        body_bytes = len(body.encode("utf-8")) if isinstance(body, str) else 0
        skills_bytes += body_bytes
        loaded_ids.append(name)
    channels["required_skill"] = {
        "artifacts": [{"artifact_id": n} for n in loaded_ids],
        "bytes": skills_bytes,
        "estimated_tokens": _token_estimate(skills_bytes),
    }

    prompt = (effective or {}).get("prompt") or ""
    prompt_bytes = len(prompt.encode("utf-8"))
    channels["prompt"] = {"bytes": prompt_bytes, "estimated_tokens": _token_estimate(prompt_bytes)}

    common_sha = (effective or {}).get("common_sha256")
    common_bytes = Path(plan_common_path).stat().st_size if plan_common_path and Path(plan_common_path).exists() else None
    channels["external_knowledge"] = {
        "bytes": common_bytes, "estimated_tokens": _token_estimate(common_bytes or 0),
        "runtime_common_sha256": common_sha,
    }
    tools = (effective or {}).get("tools") or []
    tools_bytes = len(json.dumps(tools).encode("utf-8"))
    channels["tools"] = {"count": len(tools), "bytes": tools_bytes, "estimated_tokens": _token_estimate(tools_bytes)}

    # Misdelivery detection: anything loaded at runtime outside the contract.
    misdeliveries: list[dict[str, Any]] = []
    if contract is not None and not disabled:
        contracted = {a["artifact_id"] for a in contract["artifacts"]}
        for aid in loaded_ids:
            if aid not in contracted:
                misdeliveries.append({"channel": "required_skill", "artifact_id": aid,
                                      "reason": "loaded_artifact_not_in_contract"})
        for art in contract["artifacts"]:
            if art["channel"] == "required_skill" and art["kind"] == "skill_card" \
                    and art["artifact_id"] not in loaded_ids:
                misdeliveries.append({"channel": "required_skill", "artifact_id": art["artifact_id"],
                                      "reason": "contract_artifact_not_delivered"})
        if "__common__" in contracted and plan_common_path and Path(plan_common_path).exists():
            expected = next(a for a in contract["artifacts"] if a["artifact_id"] == "__common__")
            actual = sha256_file(Path(plan_common_path))
            if actual != expected["digest"]:
                misdeliveries.append({
                    "channel": "external_knowledge", "reason": "delivered_digest_differs_from_contract",
                    "contract_digest": expected["digest"], "materialized_digest": actual,
                })

    budget = (contract or {}).get("budget") or DEFAULT_BUDGET
    total_bytes = sum(c["bytes"] or 0 for c in channels.values())
    budget_state = {
        "total_bytes": total_bytes,
        "total_budget": budget.get("total_bytes", DEFAULT_BUDGET["total_bytes"]),
        "within_budget": total_bytes <= budget.get("total_bytes", DEFAULT_BUDGET["total_bytes"]),
        "per_channel": {},
        "delivery_refused": bool(misdeliveries) and not disabled,
    }
    for name, chan in channels.items():
        limit = budget.get(f"{name}_bytes")
        if limit is not None:
            budget_state["per_channel"][name] = {
                "bytes": chan["bytes"], "budget": limit, "within": (chan["bytes"] or 0) <= limit,
            }

    failures = {
        "coverage_failures": [r.get("reason") for r in records if r.get("kind") == "coverage_failure"],
        "node_failures": [r.get("node") for r in records
                          if r.get("kind") == "node_result" and r.get("result") is None],
        "run_error": result.get("error"),
        "returncode": result.get("returncode"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "qid": result.get("qid", qdir.name[1:] if qdir.name.startswith("q") else qdir.name),
        "delivery_disabled": disabled,
        "bundle_id": (contract or {}).get("bundle_id"),
        "channels": channels,
        "usage_actual": usage_actual,
        "estimated_delivered_tokens": sum(c["estimated_tokens"] for c in channels.values()),
        "misdeliveries": misdeliveries,
        "budget": budget_state,
        "failures": failures,
        "s_agent": result.get("s_agent"),
        "steps": result.get("steps"),
        "trace_complete": result.get("trace_complete"),
        "delivery_mode_reported": "full_delivery",  # never reported as recall hit
    }


def aggregate_ledgers(ledgers: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize ledger rows for a batch (no per-question content)."""
    n = len(ledgers)
    if not n:
        return {"runs": 0}
    return {
        "runs": n,
        "clean_runs": sum(1 for l in ledgers
                          if l["failures"]["returncode"] in (0, None) and not l["failures"]["run_error"]),
        "runs_with_misdelivery": sum(1 for l in ledgers if l["misdeliveries"]),
        "runs_over_budget": sum(1 for l in ledgers if not l["budget"]["within_budget"]),
        "mean_delivered_bytes": round(sum(l["budget"]["total_bytes"] for l in ledgers) / n, 1),
        "mean_estimated_tokens": round(sum(l["estimated_delivered_tokens"] for l in ledgers) / n, 1),
        "total_actual_input_tokens": sum((l["usage_actual"].get("input_tokens") or 0) for l in ledgers),
        "total_actual_output_tokens": sum((l["usage_actual"].get("output_tokens") or 0) for l in ledgers),
        "total_actual_requests": sum((l["usage_actual"].get("requests") or 0) for l in ledgers),
        "coverage_failures": sum(len(l["failures"]["coverage_failures"]) for l in ledgers),
        "runs_with_run_error": sum(1 for l in ledgers if l["failures"]["run_error"]),
    }
