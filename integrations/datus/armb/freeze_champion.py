"""POWE-137 ARM-B: freeze the champion BEFORE any old-R run starts.

Writes a freeze record binding the champion skill directory to an exact
tree digest + SKILL.md sha256 + UTC timestamp, plus the selection evidence
(D-side scores that justified promotion). The freeze timestamp must precede
the first R run's directory mtime; the R evaluation reads this record only
after it was written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent / "powercontext"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "integrations" / "datus" / "src"))

from powercontext_datus.freeze import digest_json, snapshot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cand", required=True)
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--selection-evidence", required=True, help="JSON: D-side summary justifying the champion")
    parser.add_argument("--out", required=True)
    options = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    skill_dir = root / "skills" / options.cand / options.skill_name
    files = snapshot(skill_dir)
    body = (skill_dir / "SKILL.md").read_bytes()
    record = {
        "arm": "B",
        "task": "POWE-137",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": options.cand,
        "skill_name": options.skill_name,
        "skill_dir": str(skill_dir.relative_to(root)),
        "tree_digest": digest_json(files),
        "skill_md_sha256": hashlib.sha256(body).hexdigest(),
        "files": files,
        "selection_evidence": json.loads(Path(options.selection_evidence).read_text(encoding="utf-8")),
        "note": "champion frozen before any old-R question was run; R plans reference this record",
    }
    Path(options.out).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"champion frozen: {options.cand} tree_digest={record['tree_digest'][:16]}... at {record['frozen_at']}")


if __name__ == "__main__":
    sys.exit(main())
