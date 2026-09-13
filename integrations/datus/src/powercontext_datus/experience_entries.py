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

"""S3 guarded experience entries (POWE-152, DESIGN-7 P2).

Minimal experience governance: every entry is distilled only from allowed
exposed learning evidence (E1/E2/D2 and recorded round feedback), carries the
C4 guard structure verbatim (applicability, authorized schema/business
semantics, required tools, output granularity, NULL/empty-set semantics,
mismatch fallback), and is delivered through ``external_knowledge`` — the
existing native surface.  No per-task provider is built here; that stays
behind the S3 content-increment proof.

Entry lifecycle recorded in the governance manifest:
``source`` -> ``candidate`` (this synthesis) -> ``approved revision``
(freeze before any run).  POWE-148 evidence (J mean 3/23 with unguarded
injection) is why the guard fields are mandatory, not advisory.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

SCHEMA_VERSION = "powercontext.experience/1"

# Inherited from the frozen C4 guard block; entries reference these by id and
# cannot weaken them.
C4_GUARDS = [
    "G-EXACT-OUTPUT", "G-NO-ROUND", "G-DISTINCT-LIST", "G-SCOPE-DENOM",
    "G-SEMANTIC-BINDING", "G-TIME-BOUNDARY", "G-GROUP-KEY", "G-NULL-EMPTY",
]


def _entry(entry_id: str, title: str, *, applicability, scope, tools, procedure,
           output, null_empty, fallback, source, extra_guards=(), structure=None):
    return {
        "id": entry_id,
        "title": title,
        "applicability": applicability,       # when this entry applies (conditions)
        "scope": scope,                       # authorized schema / business semantics
        "required_tools": tools,              # what the entry needs
        "procedure": procedure,               # ordered structural steps
        "output_granularity": output,         # grain + column contract
        "null_empty_semantics": null_empty,   # NULL / empty-set handling
        "mismatch_fallback": fallback,        # what to do when conditions don't match
        "guards": list(C4_GUARDS) + list(extra_guards),
        "structure_recipe": structure or "",
        "source": source,
    }


def build_v1_entries() -> list[dict[str, Any]]:
    """One-shot extraction: structure-level recipes from D2 evidence (single pass).

    Derived from the 23 oracle-verified D2 reference SQL statements (allowed
    exposed learning evidence) plus the E2 descriptor abstractions.  These are
    *structure* recipes — generic moves that transfer to unseen questions of
    the same structural shape — not per-question answers.
    """
    D2 = "D2 reference SQL (oracle-verified, allowed learning evidence)"
    return [
        _entry(
            "EXP-01-unordered-pair-enumeration",
            "Enumerate unordered pairs of entities sharing a relation",
            applicability="any question whose grain is 'pairs of X' with a symmetric/relation table between X and Y (co-purchase, co-occurrence, similarity between entities)",
            scope="a DISTINCT edge table (entity_a, entity_b) derived from the fact table; both IDs non-NULL",
            tools=["execute_sql"],
            procedure=[
                "Build edges: SELECT DISTINCT <left_id>, <right_id> FROM <fact> WHERE both IDs non-NULL",
                "Self-join edges with a.a_id < b.b_id ON the shared key — the strict < comparison enumerates each unordered pair exactly once and excludes self-pairs",
                "Group by (a_id, b_id) to compute the shared count / intersection measure",
                "Join a per-entity size aggregate on both sides when a union denominator or set-similarity ratio is needed",
            ],
            output="one row per unordered pair; columns: entity_a, entity_b, shared, [union], [similarity rounded to 6 decimals]; ORDER BY entity_a, entity_b",
            null_empty="empty edge set -> zero rows (do not fabricate); similarity denominators that are zero cannot occur when every pair shares >=1 key",
            fallback="if the question pairs different entities per side (a->b directed), use a.a_id != b.b_id instead of < and keep direction; if it asks for top-K pairs, wrap with ORDER BY similarity DESC LIMIT K",
            source=D2 + " V2-01 Jaccard; V2-03 ordered-pair variant",
            structure="edges CTE -> self-join(a<b) -> group -> join sizes -> ratio",
        ),
        _entry(
            "EXP-02-median-and-mad",
            "Median and median absolute deviation per group (MySQL, no MEDIAN())",
            applicability="question asks for a median or median absolute deviation of a measure within groups",
            scope="one row per item with a numeric measure; window functions available",
            tools=["execute_sql"],
            procedure=[
                "Number rows within each group: ROW_NUMBER() OVER (PARTITION BY g ORDER BY measure, tiebreak_id) rn and COUNT(*) OVER (PARTITION BY g) n",
                "Median = AVG(measure) over rn IN (FLOOR((n+1)/2), FLOOR((n+2)/2)) — handles both odd and even n; GROUP BY g",
                "For MAD, join the median back per group and take the median of ABS(measure - median) by repeating steps 1-2 over the deviation column",
            ],
            output="one row per group: group key, median [, mad]; no rounding unless the question rounds",
            null_empty="groups with all-NULL measures excluded before numbering; a group with no rows simply produces no output row",
            fallback="if the dialect has a native MEDIAN/PERCENTILE_CONT, prefer it; if a mode is asked instead, use COUNT-based ranking, not this recipe",
            source=D2 + " V2-06 median Price and MAD per currency",
            structure="window numbering -> dual middle-row average -> deviation pass",
        ),
        _entry(
            "EXP-03-concentration-index",
            "Concentration / share-based index (HHI) per group",
            applicability="question asks for a concentration, Herfindahl index, or sum of squared shares within groups",
            scope="per-(group, member) aggregate value v > 0; group total via window SUM",
            tools=["execute_sql"],
            procedure=[
                "Aggregate the measure to (group, member) grain first",
                "Compute the group total with SUM(v) OVER (PARTITION BY group)",
                "Index = SUM(POWER(CAST(v AS DECIMAL(60,18)) / total, 2)) GROUP BY group; round only at the final projection, to 6 decimals",
            ],
            output="one row per group: group key, member_count, index; ORDER BY group key",
            null_empty="exclude NULL members and zero/negative values before the window sum, per recorded reference behavior",
            fallback="for top-1 share or CR-ratio questions keep the same window total and take MAX(v)/total instead of squared shares",
            source=D2 + " V2-11 product-quantity HHI per currency",
            structure="pre-aggregate -> window total -> squared-share sum",
        ),
        _entry(
            "EXP-04-set-signature-equivalence",
            "Equivalence classes by set signature (GROUP_CONCAT canonicalization)",
            applicability="question groups entities by their exact set of related items ('identical sets', 'same products bought') and counts classes",
            scope="edge table (entity, item); GROUP_CONCAT available",
            tools=["execute_sql"],
            procedure=[
                "Build DISTINCT (entity, item) edges",
                "Per entity: COUNT(*) items and GROUP_CONCAT(item ORDER BY item SEPARATOR ',') as the canonical signature — ordering inside GROUP_CONCAT makes equal sets produce equal strings",
                "Group by (item_count, signature) to count class size k",
                "Re-aggregate to the asked grain, e.g. GROUP BY item_count, k ORDER BY both",
            ],
            output="class histogram rows: set size, class size, number of classes",
            null_empty="entities with zero edges have empty signatures — decide from the question whether empty sets count, and say so by including/excluding them consistently",
            fallback="if the question asks for 'at least k shared items' instead of identical sets, switch to the pair-enumeration recipe (EXP-01) with a HAVING on shared count",
            source=D2 + " V2-02 equivalence classes of customers by purchased-product set",
            structure="edges -> canonical signature -> group -> re-aggregate",
        ),
        _entry(
            "EXP-05-dirty-datetime-normalization",
            "Guarded timestamp construction from string Date/Time columns",
            applicability="any computation needing a real timestamp from separate string date and time columns that may contain malformed values",
            scope="Date DATE strings and Time VARCHAR strings; TIMESTAMP(date,time) constructor",
            tools=["execute_sql"],
            procedure=[
                "Construct conditionally: CASE WHEN Date IS NOT NULL AND Time REGEXP '^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$' THEN TIMESTAMP(Date, Time) ELSE NULL END",
                "Filter malformed strings with REGEXP before arithmetic; never compare raw strings when a timestamp is meant",
                "For month strings like YYYYMM validate with REGEXP '^[0-9]{4}(0[1-9]|1[0-2])$' and build month starts with STR_TO_DATE(CONCAT(Date,'01'),'%Y%m%d')",
            ],
            output="a normalized derived column inside a CTE; downstream logic uses only the normalized value",
            null_empty="malformed or NULL inputs normalize to NULL and drop out of aggregates — this is intended, recorded behavior",
            fallback="if the dialect lacks TIMESTAMP(date,time), use DATE_ADD(Date, INTERVAL time HOUR_SECOND); if Time has single-digit hours, widen the regex",
            source=D2 + " shared CTE prologue across the D2 reference set",
            structure="CASE + REGEXP guard -> normalized column",
        ),
        _entry(
            "EXP-06-two-hop-composition",
            "Two-step composition: resolve an identifier, then aggregate by it",
            applicability="question names an entity by a natural key (name/description) but the aggregation runs on its surrogate ID, or joins a lookup then a fact",
            scope="lookup table (natural key -> ID) plus fact table carrying the ID",
            tools=["execute_sql"],
            procedure=[
                "Step 1 (may be a separate tool call): resolve the natural key to ID(s) — allow several IDs (IN list) when several names are given",
                "Step 2: aggregate the fact table filtered by the resolved ID(s); count S_agent honestly as two online steps",
                "Prefer a single joined SQL when both steps fit one statement; keep the two-call form when name resolution is genuinely exploratory",
            ],
            output="final answer at the asked grain; intermediate ID rows are never part of the answer",
            null_empty="an unknown natural key resolves to zero rows — return an empty result rather than guessing",
            fallback="if the fact table already carries the natural key, skip the lookup hop; do not force two calls for a one-call question",
            source="original user story (store name -> ID -> monthly revenue) + E2 composition descriptors",
            structure="lookup -> IN filter -> aggregate",
        ),
    ]


def build_v2_entries() -> list[dict[str, Any]]:
    """Incremental refinement on top of v1, from recorded round feedback.

    Sources (all allowed exposed evidence): POWE-146 R x3 disclosure
    (q3/q23/q25 judgment-class semantic misses persisting across six rounds),
    the p2 D2-patterns card family notes, and the E2 negative set.
    """
    entries = build_v1_entries()
    entries.append(_entry(
        "EXP-07-display-rounding-discipline",
        "Compute precisely, round only at display — and never when the reference does not",
        applicability="any question whose reference emits an unrounded float or fixes a specific rounding depth",
        scope="division and ratio computations",
        tools=["execute_sql"],
        procedure=[
            "Wrap divisions as CAST(numerator AS DECIMAL(60,18)) / denominator (or AS FLOAT when the reference used FLOAT) at computation time",
            "Apply ROUND(x, d) only when the question or recorded reference contract names d — default d=6 on ratio outputs in the D2 set",
            "Never round inside ORDER BY comparisons of values that are displayed rounded",
        ],
        output="numeric columns exactly at the recorded precision",
        null_empty="ratios over zero denominators are NULL, not zero",
        fallback="if the oracle disagrees with a displayed value, suspect rounding placement first — recompute unrounded",
        source="POWE-146 disclosure (q3/q23/q25 persistent semantic misses) + D2 output contracts",
        structure="late rounding only",
    ))
    entries.append(_entry(
        "EXP-08-rank-with-unique-tiebreak",
        "Deterministic top-1 with ties",
        applicability="questions asking for 'the' maximum/minimum row when ties are possible",
        scope="window ordering or ORDER BY ... LIMIT 1 patterns",
        tools=["execute_sql"],
        procedure=[
            "Add a unique tiebreak column (primary key) to every ORDER BY used for extremum selection",
            "When the question asks for all tied rows, drop LIMIT and select rows where the measure equals the group extremum via a window MAX",
            "LIMIT 1 without a tiebreak is only acceptable when the reference itself used it — check the recorded contract",
        ],
        output="exactly the asked number of rows",
        null_empty="empty group -> no row; never emit a placeholder",
        fallback="if ordering is unstable across engines, compute the extremum first, then equality-select",
        source="T02/T08 guard notes (LIMIT 1 keeps exactly one row) + E2 negatives",
        structure="tiebreak column discipline",
    ))
    entries.append(_entry(
        "EXP-09-self-contained-final-sql",
        "Final answer binding: submit exactly the executed SQL",
        applicability="every run, before answering",
        scope="GenSQL final envelope vs executed statement",
        tools=["execute_sql"],
        procedure=[
            "Execute the SQL, then bind the final answer to the exact text executed — copy character for character including format literals",
            "Never reformat, re-indent or re-derive the submitted SQL from memory after execution",
            "If a later statement supersedes an earlier one, the last executed successful statement is the one submitted",
        ],
        output="final_sql span identical to the executed statement",
        null_empty="unknown/failed executions stay in the denominator — submit nothing rather than an unexecuted SQL",
        fallback="none — this is a submission discipline, always applicable",
        source="p2 d2-patterns card submission discipline (validated in POWE-146/149)",
        structure="submission binding",
    ))
    return entries


def render_experience_digest(entries: list[dict[str, Any]], *, base_common: str) -> str:
    """Render entries into the external_knowledge append block."""
    lines = [
        "",
        "# EXPERIENCE ENTRIES (governed, guarded; revision frozen before runs)",
        "",
        "Each entry states when it applies, the authorized semantics, the structural procedure,",
        "output contract, NULL/empty handling, and a mismatch fallback. MANDATORY RULE: if a",
        "question does not match an entry's applicability, do NOT force the entry — fall back",
        "to deriving SQL natively from the schema; the entry guards still apply to any SQL you emit.",
        "",
    ]
    for e in entries:
        lines.append(f"## {e['id']} — {e['title']}")
        lines.append(f"- Applicability: {e['applicability']}")
        lines.append(f"- Scope: {e['scope']}")
        lines.append(f"- Required tools: {', '.join(e['required_tools'])}")
        lines.append("- Procedure:")
        for i, step in enumerate(e["procedure"], 1):
            lines.append(f"  {i}. {step}")
        lines.append(f"- Output contract: {e['output_granularity']}")
        lines.append(f"- NULL/empty: {e['null_empty_semantics']}")
        lines.append(f"- Mismatch fallback: {e['mismatch_fallback']}")
        lines.append(f"- Inherits guards: {', '.join(e['guards'])}")
        lines.append(f"- Source: {e['source']}")
        lines.append("")
    return base_common.rstrip("\n") + "\n" + "\n".join(lines)


def freeze_experience(entries: list[dict[str, Any]], *, variant: str) -> dict[str, Any]:
    """Governance record: source -> candidate -> approved revision (frozen)."""
    item_digests = [
        {"id": e["id"], "sha256": hashlib.sha256(json.dumps(e, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        for e in entries
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "variant": variant,
        "governance": {
            "source": "allowed exposed learning evidence (E1/E2/D2 reference SQL, recorded round feedback); H2/H3 holdouts never read",
            "candidate": f"experience-{variant} synthesized {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "revision": "approved-rev-1 (frozen before any screening/regression run)",
            "guard_inheritance": "all entries carry the eight C4 guards; POWE-148 (J=3/23 unguarded) is the recorded reason",
        },
        "entries": item_digests,
        "set_sha256": hashlib.sha256(json.dumps(item_digests, sort_keys=True).encode()).hexdigest(),
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return manifest
