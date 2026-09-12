"""POWE-138 ARM-C: render validated descriptors into first-batch artifacts.

Rendering is mechanical from descriptors-v1.json (zero model calls). Four
candidate packagings, all injected through EXISTING access points only
(native SkillManager required-skill injection, or the shared external_knowledge
common file). No executor, no loop change:

  C1  one skill, compact template rendering
  C2  one skill, template rendering + worked evidence SQL examples
  C3  three skills split by domain area
  C4  knowledge-file packaging (external_knowledge append), no skill dir
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC = json.loads((HERE / "candidates" / "descriptors-v1.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((HERE.parent / "powercontext" / "integrations" / "datus" / "e1" / "manifest.json").read_text(encoding="utf-8"))
SKILLS = HERE / "skills"
DESCS = DOC["descriptors"]
GUARDS = DOC["guards"]

REF_SQL = {f"reference-q{e['qid']}": e["sql"] for e in MANIFEST["entries"] if e["origin"] == "REFERENCE_SQL"}

FALLBACK = (
    "FALLBACK RULE: if the question matches no template above (different entity, measure, grain or "
    "filters), do not force a template — derive the SQL natively from the schema in the system prompt. "
    "The guards above still apply to every SQL you emit, native or templated."
)

DIRECT = (
    "USAGE: the full schema is already in your context; do not call describe_table or list_tables. "
    "Match the question to one template, bind its parameters, and produce the final SQL in one step "
    "(one execute_sql call). Emit the complete answer table exactly as the output contract requires."
)


def block(d: dict, worked: bool = False) -> str:
    lines = [f"### {d['descriptor_id']} — {d['family']}", "", f"Use when: {d['applicable_when']}", ""]
    lines.append("Parameters:")
    for p in d["params"]:
        dom = f" domain={p['domain']}" if "domain" in p else ""
        bind = f" bind from: {p['bind_from']}" if "bind_from" in p else ""
        lines.append(f"- {p['name']} ({p['type']}{dom}){bind}")
    lines.append("")
    st = d["structure"]
    lines.append("Structure: joins=" + "; ".join(st.get("joins") or ["none"]) +
                 f" | group_by={st.get('group_by')} | aggregation={st.get('aggregation')}")
    if st.get("order_limit"):
        lines.append(f"Order/limit: {st['order_limit']}")
    if st.get("forms"):
        lines.append("Forms (bind exactly one):")
        for k, v in st["forms"].items():
            lines.append(f"- {k}: {v}")
    elif d.get("sql_template"):
        lines.append(f"SQL skeleton: {d['sql_template']}")
    oc = d["output_contract"]
    lines.append("Output contract: " + "; ".join(f"{k}={v}" for k, v in oc.items()))
    lines.append("")
    if worked:
        lines.append("Verified evidence SQL (exact instantiations, independently executed read-only):")
        for sid in d["evidence_positive"]:
            if sid in REF_SQL:
                lines.append(f"- {sid}: {REF_SQL[sid]}")
        lines.append("")
    negs = d.get("evidence_negative") or []
    if negs:
        lines.append("Known failure modes on this family (never reproduce them): " + ", ".join(negs))
        lines.append("")
    return "\n".join(lines)


def guards_block() -> str:
    lines = ["## Non-negotiable guards (each one is a recorded failure that lost a question)", ""]
    for gid, text in GUARDS.items():
        lines.append(f"- **{gid}**: {text}")
    return "\n".join(lines)


def skill_md(name: str, description: str, body: str) -> str:
    quoted = "'" + description.replace("'", "''") + "'"
    return f"---\nname: {name}\ndescription: {quoted}\n---\n\n{body}\n"


def write_skill(dir_name: str, name: str, description: str, body: str) -> dict:
    root = SKILLS / dir_name
    target = root / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(skill_md(name, description, body), encoding="utf-8")
    return {"skill_root": str(root), "skill_names": [name]}


def digest_dir(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(path)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def build_c1() -> dict:
    body = ("# birdbench structured query templates (descriptor rendering, ARM-C candidate C1)\n\n"
            + DIRECT + "\n\n" + guards_block() + "\n\n## Templates\n\n"
            + "\n".join(block(d) for d in DESCS) + "\n" + FALLBACK)
    return write_skill("C1", "birdbench-c-templates",
                       "Structured, counterexample-validated query templates for the birdbench fuel-card "
                       "domain: match the question to a template, bind parameters, emit SQL in one step.",
                       body)


def build_c2() -> dict:
    body = ("# birdbench structured query templates with verified examples (ARM-C candidate C2)\n\n"
            + DIRECT + "\n\n" + guards_block() + "\n\n## Templates\n\n"
            + "\n".join(block(d, worked=True) for d in DESCS) + "\n" + FALLBACK)
    return write_skill("C2", "birdbench-c-templates-ex",
                       "Structured, counterexample-validated query templates for the birdbench fuel-card "
                       "domain, each with independently verified evidence SQL instantiations.",
                       body)


def build_c3() -> dict:
    groups = {
        "birdbench-c-dim": [d for d in DESCS if d["descriptor_id"].startswith(("T01", "T06"))],
        "birdbench-c-agg": [d for d in DESCS if d["descriptor_id"].startswith(("T02", "T03", "T04", "T05"))],
        "birdbench-c-txn": [d for d in DESCS if d["descriptor_id"].startswith(("T07", "T08"))],
    }
    names = []
    root = SKILLS / "C3"
    if root.exists():
        shutil.rmtree(root)
    for name, ds in groups.items():
        area = {"birdbench-c-dim": "dimension-table filters and distinct lists",
                "birdbench-c-agg": "consumption aggregation and conditional comparison",
                "birdbench-c-txn": "transaction filtering, aggregates and point lookups"}[name]
        body = (f"# birdbench templates — {area} (ARM-C candidate C3 split skill)\n\n"
                + DIRECT + "\n\n" + guards_block() + "\n\n## Templates\n\n"
                + "\n".join(block(d) for d in ds) + "\n" + FALLBACK)
        target = root / name
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(skill_md(name, f"Structured query templates: {area}.", body), encoding="utf-8")
        names.append(name)
    return {"skill_root": str(root), "skill_names": names}


def build_c4() -> dict:
    common = (HERE / "common-birdbench.txt").read_text(encoding="utf-8")
    body = ("\n\n# STRUCTURED QUERY TEMPLATES (ARM-C candidate C4, knowledge-file packaging)\n\n"
            "The following counterexample-validated templates cover the recurring question families of "
            "this database. " + DIRECT + "\n\n" + guards_block() + "\n\n## Templates\n\n"
            + "\n".join(block(d) for d in DESCS) + "\n" + FALLBACK + "\n")
    out = HERE / "common-c4.txt"
    out.write_text(common + body, encoding="utf-8")
    return {"common_override": str(out), "skill_root": str(HERE.parent / "powercontext-empty-skills-placeholder"),
            "skill_names": []}


def main() -> None:
    packs = {
        "C1": {**build_c1(), "packaging": "required-skill injection (single skill, compact rendering)"},
        "C2": {**build_c2(), "packaging": "required-skill injection (single skill, + worked evidence SQL)"},
        "C3": {**build_c3(), "packaging": "required-skill injection (three split skills)"},
        "C4": {**build_c4(), "packaging": "external_knowledge common-file append (no skill directory)"},
    }
    reg = {"descriptor_digest": hashlib.sha256((HERE / 'candidates' / 'descriptors-v1.json').read_bytes()).hexdigest(),
           "candidates": {}}
    for cid, p in packs.items():
        entry = dict(p)
        if p.get("skill_root") and "placeholder" not in p["skill_root"]:
            entry["content_digest"] = digest_dir(Path(p["skill_root"]))
        elif p.get("common_override"):
            entry["content_digest"] = hashlib.sha256(Path(p["common_override"]).read_bytes()).hexdigest()
        reg["candidates"][cid] = entry
    (HERE / "candidates" / "candidate-registry.json").write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    for cid, p in packs.items():
        size = sum(f.stat().st_size for f in Path(p.get("skill_root", HERE)).rglob("SKILL.md")) if "placeholder" not in p.get("skill_root", "x") else Path(p["common_override"]).stat().st_size
        print(cid, p["packaging"], "bytes~", size, "digest", reg["candidates"][cid]["content_digest"][:12])


if __name__ == "__main__":
    main()
