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

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check_workflow_actions.py"


def run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *(str(path) for path in paths)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_workflow_action_checker_accepts_sha_pins_and_local_actions(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        """
jobs:
  check:
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v1
      - uses: ./.github/actions/setup-python-env
""".lstrip(),
        encoding="utf-8",
    )

    result = run_checker(workflow)

    assert result.returncode == 0, result.stderr
    assert "2 action references checked" in result.stdout


def test_workflow_action_checker_rejects_mutable_refs(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        """
jobs:
  check:
    steps:
      - uses: actions/checkout@v7
""".lstrip(),
        encoding="utf-8",
    )

    result = run_checker(workflow)

    assert result.returncode == 1
    assert "must use a 40-character commit SHA" in result.stderr


def test_workflow_action_checker_scans_nested_composite_actions(tmp_path: Path) -> None:
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "jobs:\n  check:\n    steps:\n      - uses: ./.github/actions/nested\n",
        encoding="utf-8",
    )
    action = tmp_path / ".github" / "actions" / "nested" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-python@v7\n",
        encoding="utf-8",
    )

    result = run_checker(tmp_path / ".github")

    assert result.returncode == 1
    assert "setup-python@v7" in result.stderr


def test_repository_workflow_actions_are_pinned() -> None:
    result = run_checker(REPOSITORY_ROOT / ".github" / "workflows", REPOSITORY_ROOT / ".github" / "actions")

    assert result.returncode == 0, result.stderr
    assert "action references checked" in result.stdout
