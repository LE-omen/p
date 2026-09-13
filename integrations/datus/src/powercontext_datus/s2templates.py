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

"""S2 typed reference templates (POWE-152, DESIGN-7 P1).

Projects the frozen C4 knowledge-file templates (T01-T08, from the frozen
p2-additive champion evidence) into datus 0.4.0 native reference templates so
the same content can be consumed directly through
``render_reference_template`` / ``execute_reference_template`` instead of as
rendered prompt text.

Safety subset (DESIGN-7 S2 clauses, non-negotiable):

* every parameter is typed: enum values, regex-anchored literals (dates,
  months), integers, or booleans — never free SQL fragments;
* string literals are escaped with MySQL single-quote doubling and rejected
  outright on control characters, ``;`` or NUL — identifiers come only from
  per-template whitelists;
* each template renders exactly one read-only SELECT on whitelisted tables —
  verified again *after* rendering (defense in depth against anything that
  slipped through parameters);
* multi-statement, comments, DML/DDL, non-whitelisted tables => reject;
* store projection is opt-in (``project_store``); the datus node only exposes
  the native tools when the store is non-empty and the harness passes the
  ``reference_template_tools`` flag (default OFF).

A Jinja sandbox render is NOT SQL parameter binding: that is why parameters
are constrained to a literal/identifier subset *before* render and the
rendered SQL is re-validated *after* render.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "powercontext.s2templates/1"

ALLOWED_TABLES = {
    "customers", "gasstations", "yearmonth", "products", "transactions_1k",
}

SQL_COMMENT_RE = re.compile(r"--|/\*|\*/|#")
SQL_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call|"
    r"load_file|into\s+outfile|into\s+dumpfile|set\s+global|prepare|execute\s+immediate)\b",
    re.IGNORECASE,
)
YM_RE = re.compile(r"^\d{4}(0[1-9]|1[0-2])$")
YEAR_RE = re.compile(r"^\d{4}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")
DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")
SEGMENTS = ["KAM", "LAM", "SME"]
CURRENCIES = ["EUR", "CZK"]
COUNTRIES = ["CZE", "SVK"]


class TemplateSafetyError(ValueError):
    """A template, parameter set, or rendered SQL fell outside the safe subset."""


def sql_string_literal(value: str) -> str:
    """Serialize a string as one MySQL literal; reject unsafe content."""
    if not isinstance(value, str):
        raise TemplateSafetyError(f"string parameter must be str, got {type(value).__name__}")
    if "\x00" in value or ";" in value or any(ord(c) < 0x20 for c in value):
        raise TemplateSafetyError(f"unsafe characters in string parameter: {value!r}")
    if "\\" in value:
        raise TemplateSafetyError("backslash metacharacter rejected in string parameter")
    return "'" + value.replace("'", "''") + "'"


def validate_param(spec: dict[str, Any], value: Any) -> Any:
    """Validate one parameter against its spec; return a normalized value."""
    name, kind = spec["name"], spec.get("type", "string")
    if value is None:
        if spec.get("required", False):
            raise TemplateSafetyError(f"missing required parameter: {name}")
        return None
    if kind == "enum":
        if value not in spec["domain"]:
            raise TemplateSafetyError(f"parameter {name}={value!r} outside domain {spec['domain']}")
        return value
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise TemplateSafetyError(f"parameter {name} must be an integer")
        if "min" in spec and value < spec["min"]:
            raise TemplateSafetyError(f"parameter {name} below min")
        if "max" in spec and value > spec["max"]:
            raise TemplateSafetyError(f"parameter {name} above max")
        return value
    if kind == "decimal":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TemplateSafetyError(f"parameter {name} must be numeric")
        s = repr(value)
        if not DECIMAL_RE.match(s):
            raise TemplateSafetyError(f"parameter {name} not a plain decimal")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise TemplateSafetyError(f"parameter {name} must be boolean")
        return value
    if kind in {"ym", "year", "date", "time"}:
        value = str(value)
        pattern = {"ym": YM_RE, "year": YEAR_RE, "date": DATE_RE, "time": TIME_RE}[kind]
        if not pattern.match(value):
            raise TemplateSafetyError(f"parameter {name}={value!r} fails {kind} format")
        return sql_string_literal(value)
    if kind == "string":
        return sql_string_literal(str(value))
    raise TemplateSafetyError(f"unknown parameter type {kind} for {name}")


def validate_template_body(body: str) -> None:
    """The unrendered template must itself stay in the safe subset."""
    if SQL_COMMENT_RE.search(body):
        raise TemplateSafetyError("SQL comments are rejected in template bodies")
    if SQL_FORBIDDEN_RE.search(body):
        raise TemplateSafetyError("forbidden SQL keyword in template body")
    if ";" in body.replace("';'", "").replace("''", ""):
        # Semicolons are only ever produced by quoting; template bodies carry none.
        raise TemplateSafetyError("multi-statement templates are rejected")
    if not body.lstrip().lower().startswith("select"):
        raise TemplateSafetyError("template must be a single SELECT")
    for table in re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", body, re.IGNORECASE):
        if table.lower() not in ALLOWED_TABLES:
            raise TemplateSafetyError(f"table {table} outside whitelist")


def validate_rendered(sql: str) -> None:
    """Post-render validation: same rules, now on the final SQL text."""
    if SQL_COMMENT_RE.search(sql):
        raise TemplateSafetyError("rendered SQL contains comment syntax")
    if SQL_FORBIDDEN_RE.search(sql):
        raise TemplateSafetyError("rendered SQL contains a forbidden keyword")
    if ";" in sql.rstrip().rstrip(";"):
        raise TemplateSafetyError("rendered SQL is multi-statement")
    if not sql.lstrip().lower().startswith("select"):
        raise TemplateSafetyError("rendered SQL is not a SELECT")
    for table in re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE):
        if table.lower() not in ALLOWED_TABLES:
            raise TemplateSafetyError(f"rendered SQL references {table} outside whitelist")
    if sql.count("'") % 2 != 0:
        raise TemplateSafetyError("rendered SQL has an unbalanced quote")


# ---------------------------------------------------------------------------
# Frozen C4 templates T01-T08 projected into typed datus reference templates.
# Every entry: concrete Jinja body over enum/regex parameters only.
# ---------------------------------------------------------------------------

_T01 = """SELECT COUNT({{ pk }}) FROM {% if table == 'customers' %}customers{% else %}gasstations{% endif %}
{%- set emitted = [False] %}
{%- if col1 %}{% set _ = emitted.append(True) %} WHERE {{ col1 }} = {{ v1 }}
{%- if col2 %} AND {{ col2 }} = {{ v2 }}{% endif %}{% endif %}"""

_T02 = """SELECT T1.{{ selector }}{% if with_total %}, SUM(T2.Consumption){% endif %}
FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID
{%- set w = [] %}
{%- if segment %}{% set _ = w.append('T1.Segment = ' ~ segment) %}{% endif %}
{%- if currency %}{% set _ = w.append('T1.Currency = ' ~ currency) %}{% endif %}
{%- if period == 'year' %}{% set _ = w.append("SUBSTR(T2.Date, 1, 4) = " ~ year) %}{% endif %}
{%- if period == 'month' %}{% set _ = w.append("T2.Date = " ~ month) %}{% endif %}
{%- if w %} WHERE {{ w | join(' AND ') }}{% endif %}
GROUP BY T1.{{ selector }} ORDER BY SUM(T2.Consumption) {% if direction == 'max' %}DESC{% else %}ASC{% endif %} LIMIT 1"""

_T03 = """SELECT T1.Segment FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID
GROUP BY T1.Segment ORDER BY SUM(T2.Consumption) {% if direction == 'max' %}DESC{% else %}ASC{% endif %} LIMIT 1"""

_T04 = """SELECT SUBSTR(T2.Date, 5, 2) FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID
WHERE SUBSTR(T2.Date, 1, 4) = {{ year }} AND T1.Segment = {{ segment }}
GROUP BY SUBSTR(T2.Date, 5, 2) ORDER BY SUM(T2.Consumption) DESC LIMIT 1"""

_T05A = """SELECT SUM(IF({{ attr }} = {{ va }},1,0)) - SUM(IF({{ attr }} = {{ vb }},1,0)) FROM customers"""

_T05B = """SELECT SUM(IF({{ attr }} = {{ va }},1,0)) - SUM(IF({{ attr }} = {{ vb }},1,0)) FROM gasstations"""

_T05C = """SELECT SUM(IF(Date = {{ ya }}, Consumption, 0)) - SUM(IF(Date = {{ yb }}, Consumption, 0))
FROM yearmonth"""

_T05D = """SELECT CAST(SUM(IF({{ attr }} = {{ va }},1,0)) AS FLOAT) / SUM(IF({{ attr }} = {{ vb }},1,0)) FROM customers"""

_T06 = """SELECT DISTINCT T3.{{ target_col }}
FROM transactions_1k AS T1
INNER JOIN {{ target_table }} AS T3 ON T1.{{ target_key }} = T3.{{ target_pk }}
{%- set w = [] %}
{%- if country %}{% set _ = w.append('T3.Country = ' ~ country) if target_col == 'ChainID' %}{% endif %}
{%- if morning %}{% set _ = w.append("T1.Time < '13:00:00'") %}{% endif %}
{%- if w %} WHERE {{ w | join(' AND ') }}{% endif %}"""

_T07 = """SELECT {% if agg == 'count' %}COUNT(TransactionID){% else %}AVG(Price){% endif %} FROM transactions_1k AS T1
{%- if country %} INNER JOIN gasstations AS T2 ON T1.GasStationID = T2.GasStationID{% endif %}
{%- set w = [] %}
{%- if country %}{% set _ = w.append('T2.Country = ' ~ country) %}{% endif %}
{%- if day %}{% set _ = w.append('T1.Date = ' ~ day) %}{% endif %}
{%- if morning %}{% set _ = w.append("T1.Time < '13:00:00'") %}{% endif %}
{%- if w %} WHERE {{ w | join(' AND ') }}{% endif %}"""

_T08C = """SELECT CustomerID FROM transactions_1k WHERE Date = {{ day }}
GROUP BY CustomerID ORDER BY SUM(Price) DESC LIMIT 1"""

_T08A = """SELECT T3.{{ target_col }} FROM transactions_1k AS T1
INNER JOIN {{ target_table }} AS T3 ON T1.{{ target_key }} = T3.{{ target_pk }}
WHERE T1.Date = {{ day }} AND T1.Time = {{ moment }}"""


def _enum(name, domain, required=True, desc="", role="identifier"):
    return {"name": name, "type": "enum", "domain": list(domain), "required": required,
            "description": desc, "role": role}


def _lit(name, kind, required=False, desc=""):
    return {"name": name, "type": kind, "required": required, "description": desc}


def _str(name, required=False, desc=""):
    return {"name": name, "type": "string", "required": required, "description": desc}


def _bool(name, required=False, desc=""):
    return {"name": name, "type": "boolean", "required": required, "description": desc}


def build_template_set() -> list[dict[str, Any]]:
    """The frozen C4-derived typed templates (same content, tool-consumable).

    hops=1 single-table families, hops=2 join families — the minimal
    comparison must cover at least one of each (DESIGN-7 S2).
    """
    t = []
    t.append({
        "name": "t01-dim-filter-count", "subject_path": ["birdbench", "dimensions"],
        "family": "T01-dim-filter-count", "hops": 1,
        "summary": "Count dimension rows (customers/gasstations) with 0-2 attribute equalities.",
        "search_text": "how many customers gas stations segment currency country count dimension filter",
        "template": _T01,
        "parameters": [
            _enum("table", ["customers", "gasstations"], desc="target dimension table"),
            _enum("pk", ["CustomerID", "GasStationID"], required=False, desc="primary key to count; default derived from table"),
            _enum("col1", ["Segment", "Currency", "Country"], required=False, desc="first filter column"),
            _str("v1", desc="first filter value (exact case)"),
            _enum("col2", ["Segment", "Currency", "Country"], required=False, desc="second filter column"),
            _str("v2", desc="second filter value"),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-NULL-EMPTY"],
    })
    t.append({
        "name": "t02-cust-ym-consumption-extremum", "subject_path": ["birdbench", "consumption"],
        "family": "T02-cust-ym-consumption-extremum", "hops": 2,
        "summary": "Customer (or segment) with the most/least consumption in yearmonth, optional filters and total.",
        "search_text": "consumed most least gas customer segment year month total yearmonth extremum",
        "template": _T02,
        "parameters": [
            _enum("selector", ["CustomerID", "Segment"], desc="projected grouping"),
            _enum("direction", ["max", "min"], desc="extremum direction"),
            _enum("segment", SEGMENTS, required=False, desc="segment value", role="value"),
            _enum("currency", CURRENCIES, required=False, desc="currency value", role="value"),
            _enum("period", ["none", "year", "month"], required=False, desc="time scope; default none"),
            _lit("year", "year", desc="YYYY when period=year"),
            _lit("month", "ym", desc="YYYYMM when period=month"),
            _bool("with_total", desc="also project SUM(Consumption)"),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-GROUP-KEY"],
    })
    t.append({
        "name": "t03-segment-extremum", "subject_path": ["birdbench", "consumption"],
        "family": "T03-segment-agg-extremum", "hops": 2,
        "summary": "Segment with least/most total consumption.",
        "search_text": "which segment least most total consumption",
        "template": _T03,
        "parameters": [_enum("direction", ["max", "min"])],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-GROUP-KEY"],
    })
    t.append({
        "name": "t04-peak-month", "subject_path": ["birdbench", "consumption"],
        "family": "T04-peak-month", "hops": 2,
        "summary": "Peak consumption month (MM) of a segment within a year.",
        "search_text": "peak busiest month consumption segment year",
        "template": _T04,
        "parameters": [_enum("segment", SEGMENTS, desc="segment value", role="value"), _lit("year", "year", required=True)],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-GROUP-KEY"],
    })
    t.append({
        "name": "t05-diff-customers", "subject_path": ["birdbench", "comparison"],
        "family": "T05-conditional-agg", "hops": 1,
        "summary": "How many more customers with attribute va than vb (single difference value).",
        "search_text": "how many more customers than difference segment currency",
        "template": _T05A,
        "parameters": [
            _enum("attr", ["Segment", "Currency"]),
            _str("va", required=True), _str("vb", required=True),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-SCOPE-DENOM"],
    })
    t.append({
        "name": "t05-diff-gasstations", "subject_path": ["birdbench", "comparison"],
        "family": "T05-conditional-agg", "hops": 1,
        "summary": "How many more gas stations with attribute va than vb.",
        "search_text": "how many more gas stations than difference country segment",
        "template": _T05B,
        "parameters": [
            _enum("attr", ["Country", "Segment"]),
            _str("va", required=True), _str("vb", required=True),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-SCOPE-DENOM"],
    })
    t.append({
        "name": "t05-ym-diff", "subject_path": ["birdbench", "comparison"],
        "family": "T05-conditional-agg", "hops": 1,
        "summary": "Consumption difference between two months (YYYYMM) over all customers.",
        "search_text": "difference consumption between two months yearmonth",
        "template": _T05C,
        "parameters": [_lit("ya", "ym", required=True), _lit("yb", "ym", required=True)],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND"],
    })
    t.append({
        "name": "t05-ratio-customers", "subject_path": ["birdbench", "comparison"],
        "family": "T05-conditional-agg", "hops": 1,
        "summary": "Ratio of customers with attribute va to customers with vb (exact float).",
        "search_text": "ratio of customers to segment currency",
        "template": _T05D,
        "parameters": [
            _enum("attr", ["Segment", "Currency"]),
            _str("va", required=True), _str("vb", required=True),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-SCOPE-DENOM"],
    })
    t.append({
        "name": "t06-distinct-list-join", "subject_path": ["birdbench", "listing"],
        "family": "T06-distinct-list-join", "hops": 2,
        "summary": "Distinct values (chain/description/currency/segment) reachable from transactions.",
        "search_text": "list what kind of which chains descriptions currencies segments transactions",
        "template": _T06,
        "parameters": [
            _enum("target_col", ["ChainID", "Description", "Currency", "Segment"], desc="listed attribute"),
            _enum("country", COUNTRIES, required=False, desc="chain country filter (ChainID only)", role="value"),
            _bool("morning", desc="Time < 13:00:00 filter"),
        ],
        "guards": ["G-DISTINCT-LIST", "G-TIME-BOUNDARY", "G-NULL-EMPTY"],
    })
    t.append({
        "name": "t07-txn-aggregate", "subject_path": ["birdbench", "transactions"],
        "family": "T07-txn-filtered-aggregate", "hops": 2,
        "summary": "Count transactions or average price over a filtered transaction set (country/date/morning).",
        "search_text": "how many transactions average price country date morning",
        "template": _T07,
        "parameters": [
            _enum("agg", ["count", "avg_price"]),
            _enum("country", COUNTRIES, required=False, role="value"),
            _lit("day", "date", desc="YYYY-MM-DD"),
            _bool("morning", desc="Time < '13:00:00'"),
        ],
        "guards": ["G-SEMANTIC-BINDING", "G-TIME-BOUNDARY", "G-NO-ROUND", "G-NULL-EMPTY"],
    })
    t.append({
        "name": "t08-top-of-date", "subject_path": ["birdbench", "transactions"],
        "family": "T08-point-lookup", "hops": 1,
        "summary": "Customer who paid the most on one date (SUM(Price) DESC LIMIT 1).",
        "search_text": "who paid the most on date highest spending single day",
        "template": _T08C,
        "parameters": [_lit("day", "date", required=True)],
        "guards": ["G-EXACT-OUTPUT", "G-NO-ROUND", "G-GROUP-KEY"],
    })
    t.append({
        "name": "t08a-timestamp-lookup", "subject_path": ["birdbench", "transactions"],
        "family": "T08-point-lookup", "hops": 2,
        "summary": "Attribute of the transaction at an exact date+time, resolved through a join (segment/country/chain/description).",
        "search_text": "at hh:mm:ss on date which segment country chain description transaction point lookup",
        "template": _T08A,
        "parameters": [
            _enum("target_col", ["Segment", "Currency", "ChainID", "Description"], desc="asked attribute"),
            _lit("day", "date", required=True, desc="YYYY-MM-DD"),
            _lit("moment", "time", required=True, desc="HH:MM:SS exact time"),
        ],
        "guards": ["G-EXACT-OUTPUT", "G-SEMANTIC-BINDING"],
    })
    return t


# Target column -> join wiring for T06/T08A (identifier whitelist, not params).
TARGET_WIRING = {
    "ChainID": {"target_table": "gasstations", "target_key": "GasStationID", "target_pk": "GasStationID"},
    "Description": {"target_table": "products", "target_key": "ProductID", "target_pk": "ProductID"},
    "Currency": {"target_table": "customers", "target_key": "CustomerID", "target_pk": "CustomerID"},
    "Segment": {"target_table": "customers", "target_key": "CustomerID", "target_pk": "CustomerID"},
}


def render_template(entry: dict[str, Any], params: dict[str, Any]) -> str:
    """Validate params, render in the datus Jinja sandbox, re-validate the SQL.

    This mirrors what ``render_reference_template`` does inside datus, plus the
    parameter whitelist that datus itself cannot know about.
    """
    from jinja2.sandbox import SandboxedEnvironment
    import jinja2

    validated: dict[str, Any] = {}
    specs = {s["name"]: s for s in entry["parameters"]}
    for name, spec in specs.items():
        value = validate_param(spec, params.get(name))
        if spec.get("type") == "enum" and spec.get("role") == "value" and value is not None:
            value = sql_string_literal(value)
        validated[name] = value
    unknown = set(params) - set(specs)
    if unknown:
        raise TemplateSafetyError(f"unknown parameters: {sorted(unknown)}")

    render_ctx = dict(validated)
    if entry["name"].startswith(("t06", "t08a")):
        wiring = TARGET_WIRING[validated.get("target_col") or entry["parameters"][0]["domain"][0]]
        render_ctx.update(wiring)
        # T01 derives pk from table when not given.
    if entry["name"].startswith("t01"):
        if not params.get("pk"):
            render_ctx["pk"] = "CustomerID" if validated.get("table") == "customers" else "GasStationID"
        if validated.get("col1") and validated.get("v1") is None:
            raise TemplateSafetyError("v1 required when col1 given")

    env = SandboxedEnvironment(undefined=jinja2.StrictUndefined)
    try:
        sql = env.from_string(entry["template"]).render(**render_ctx)
    except jinja2.UndefinedError as e:
        raise TemplateSafetyError(f"missing parameter: {e}") from e
    sql = " ".join(sql.split())
    validate_rendered(sql)
    return sql


def freeze_template_set(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze manifest: per-template digests + set digest (before any run)."""
    import time

    items = []
    for e in entries:
        validate_template_body(e["template"])
        body_digest = hashlib.sha256(e["template"].encode()).hexdigest()
        spec_digest = hashlib.sha256(json.dumps(e["parameters"], sort_keys=True).encode()).hexdigest()
        items.append({"name": e["name"], "family": e["family"], "hops": e["hops"],
                      "body_sha256": body_digest, "parameters_sha256": spec_digest,
                      "guards": e.get("guards", [])})
    set_digest = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "C4 knowledge file T01-T08 (frozen p2-additive champion evidence)",
        "templates": items,
        "set_sha256": set_digest,
    }


def project_store(home_dir: Path, entries: list[dict[str, Any]], *, datasource: str = "birdbench") -> dict[str, Any]:
    """Materialize the frozen templates into a datus reference_template store.

    The store is anchored at ``home_dir`` (the per-question ``home/.datus``),
    so projection is isolated per question and fully accounted for in the S1
    ledger as a template-store artifact.
    """
    import importlib

    home = Path(home_dir)
    home.mkdir(parents=True, exist_ok=True)
    qdir = home.parent.parent  # home_dir is <qdir>/home/.datus, matching the runner's anchoring
    AC = importlib.import_module("datus.configuration.agent_config").AgentConfig
    cfg = AC(
        nodes={}, home=str(home), project_root=str(qdir), session_dir=str(qdir / "sessions"),
        plugins_enabled=False, active_plugins={}, config_mutable=False, sql_read_only=True,
        bash={"enabled": False}, filesystem={"strict": True}, target="evaluation",
        models={"evaluation": {"type": "openai", "model": "unused", "base_url": "unused",
                               "temperature": 0, "api_key": "unused", "save_llm_trace": False, "max_retry": 1}},
        agentic_nodes={"gen_sql": {"model": "evaluation", "skills": "*", "subagents": "", "max_turns": 8, "mcp": ""}},
        services={"datasources": {datasource: {"type": "mysql", "host": "unused", "port": "3306",
                                               "username": "unused", "database": datasource}}},
    )
    cfg.current_datasource = datasource
    store_mod = importlib.import_module("datus.storage.reference_template.store")
    rag = store_mod.ReferenceTemplateRAG(cfg)
    items = []
    for e in entries:
        items.append({
            "name": e["name"],
            "template": e["template"],
            "parameters": json.dumps([{k: v for k, v in s.items() if k != "description"} for s in e["parameters"]]),
            "summary": e["summary"] + " | guards: " + ", ".join(e.get("guards", [])),
            "search_text": e["search_text"],
            "subject_path": e["subject_path"],
            "tags": "s2,frozen," + e["family"],
        })
    rag.store_batch(items)
    rag.after_init()
    return {"projected": len(items), "store_size": rag.get_reference_template_size()}


def offline_negative_cases() -> list[dict[str, Any]]:
    """The S2 offline negative suite (DESIGN-7): every case must be rejected."""
    entries = {e["name"]: e for e in build_template_set()}
    cases = [
        # (template, params, label, expect_reject)
        ("t01-dim-filter-count", {"table": "customers", "col1": "Segment", "v1": "KAM' OR '1'='1"},
         "param-injection-quote-escape-neutralized", False),
        ("t01-dim-filter-count", {"table": "customers", "col1": "Segment", "v1": "KAM; DROP TABLE customers"},
         "param-injection-semicolon", True),
        ("t01-dim-filter-count", {"table": "(SELECT 1)", "pk": "CustomerID"},
         "enum-escape-table", True),
        ("t01-dim-filter-count", {"table": "customers", "col1": "Segment", "v1": "KAM--"},
         "param-comment-smuggle", True),
        ("t01-dim-filter-count", {"table": "customers", "col1": "Segment", "v1": "KAM\\'"},
         "param-backslash-metachar", True),
        ("t02-cust-ym-consumption-extremum",
         {"selector": "CustomerID", "direction": "max", "period": "month", "month": "2012-08'; DELETE FROM yearmonth"},
         "regex-anchored-month-injection", True),
        ("t02-cust-ym-consumption-extremum", {"selector": "CustomerID", "direction": "max"},
         "valid-minimal-render", False),
        ("t04-peak-month", {"segment": "KAM", "year": "99999"}, "year-format-reject", True),
        ("t05-diff-customers", {"attr": "Segment", "va": "KAM", "vb": "SME"}, "valid-diff", False),
        ("t06-distinct-list-join", {"target_col": "Description"}, "valid-distinct-list", False),
        ("t07-txn-aggregate", {"agg": "count", "country": "USA"}, "country-enum-reject", True),
        ("t08-top-of-date", {"day": "2012-08-01;SELECT"}, "date-regex-reject", True),
        ("t01-dim-filter-count", {"table": "customers", "col1": "Segment", "v1": "KAM", "extra": "1"},
         "unknown-param-reject", True),
    ]
    results = []
    for name, params, label, expect_reject in cases:
        try:
            sql = render_template(entries[name], params)
            rejected = False
        except TemplateSafetyError as e:
            sql, rejected = f"rejected: {e}", True
        ok = rejected == expect_reject
        results.append({"case": label, "template": name, "expect_reject": expect_reject,
                        "rejected": rejected, "ok": ok, "sql": sql[:160]})
    # Stale-version / wrong-scope / final-binding cases are enforced at the
    # bundle layer (freeze digest + scope fingerprint) and asserted separately
    # by the S1/S2 selftest.
    return results
