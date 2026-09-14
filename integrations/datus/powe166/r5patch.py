"""POWE-166 A-fix: JSON object recovery for datus llm_result2json (outer adapter).

Root cause (zero-model reproduced on H4 traces h41-S4/qV4-36, h43-S4/qV4-36,
h42-H0/qV4-36): the model's final response is prose containing a set literal
like `{5,8,9,16}` BEFORE the legal `{"sql": ...}` envelope.
datus.utils.json_utils.strip_json_str extracts from the first `{` (or first-{
to last-}), yielding the set; json_repair repairs `{5,8,9,16}` into the LIST
[5, 8, 9, 16]; llm_result2json(expected_type=dict) does not reject the list,
so gen_sql's isinstance(parsed, dict) check fails, result.sql stays null, the
SQLContext is never written, and the run dies with `No SQL context`.

Fix discipline (POWE-166 mode block, POWE-165 research section 5A): the fix
lives in the shared JSON utility / outer adapter only; the core agent loop is
not modified. This module wraps llm_result2json in-place after importing
datus, so every arm in a round-5 batch runs the SAME parser version.

Semantics of the recovery:
- Fires ONLY when the original parse did not return the requested dict
  (expected_type=dict): no behavior change on any path that already works.
- Scans the raw text for balanced top-level `{...}` spans with a
  string-literal-aware scanner (double-quoted JSON strings and backslash
  escapes are skipped, so braces inside SQL string values cannot truncate a
  span).
- Candidates are tried in order of appearance; each candidate is re-run
  through the ORIGINAL llm_result2json so the existing sql backslash scrub
  and content checks apply unchanged.
- Preference order (POWE-165 ruling: only accept an explicit object that
  satisfies the SQL output schema): pass 1 takes the first candidate dict
  carrying a non-empty string `sql` (the gen_sql submission envelope); pass 2
  falls back to the first valid dict without that requirement. No oracle
  knowledge, no qid-based selection, never a bare SELECT pickup.
- `{5,8,9,16}` fails json.loads (invalid JSON) and is skipped; the following
  `{"sql": ...}` envelope parses and wins.
- Ambiguity stays failure: if no complete span parses to an acceptable dict,
  the original (possibly None) result is returned. Truncated envelopes keep
  the incumbent json_repair completion behavior (out of scope for this fix).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_INSTALLED = False


def _iter_object_spans(text: str):
    """Yield balanced top-level {...} spans, string-aware, in order."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        start, closed = i, False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
            j += 1
        if closed:
            yield text[start : j + 1]
            i = j + 1
        else:
            i += 1


def install() -> dict:
    """Monkey-wrap datus.utils.json_utils.llm_result2json. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already": True}
    import datus.utils.json_utils as ju

    original = ju.llm_result2json

    def llm_result2json_with_recovery(llm_str, expected_type=dict):
        result = original(llm_str, expected_type)
        if expected_type is not dict or isinstance(result, dict) or llm_str is None:
            return result
        fallback = None
        for candidate in _iter_object_spans(str(llm_str)):
            try:
                json.loads(candidate)
            except Exception:
                continue
            recovered = original(candidate, expected_type)
            if isinstance(recovered, dict):
                sql = recovered.get("sql")
                if isinstance(sql, str) and sql.strip():
                    return recovered
                if fallback is None:
                    fallback = recovered
        return fallback if fallback is not None else result

    ju.llm_result2json = llm_result2json_with_recovery
    _INSTALLED = True
    return {"installed": True, "already": False}
