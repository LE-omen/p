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

import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "scan_e2e_evidence.sh"


def run_scan(tmp_path: Path, *, fake_docker: str) -> subprocess.CompletedProcess[str]:
    docker = tmp_path / "docker"
    docker.write_text(fake_docker, encoding="utf-8")
    docker.chmod(0o755)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "replay.json").write_text("{}\n", encoding="utf-8")
    diagnostics = tmp_path / "diagnostics"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "DATABASE": "sqlite",
        "DIAGNOSTICS_PATH": str(diagnostics),
        "EVIDENCE_PATH": str(evidence),
        "SCENARIO_OUTCOME": "failure",
        "TRUFFLEHOG_IMAGE": "example/trufflehog@sha256:deadbeef",
    }
    bash = shutil.which("bash")
    assert bash is not None
    command = [bash, str(SCRIPT)]
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        git_bin = Path(git).resolve().parent
        git_bash = git_bin / "bash.exe"
        assert git_bash.is_file()
        cygpath = git_bin.parent / "usr" / "bin" / "cygpath.exe"

        def to_posix(path: Path) -> str:
            return subprocess.run(
                [str(cygpath), "-u", str(path)], capture_output=True, text=True, check=True
            ).stdout.strip()

        bash = str(git_bash)
        jq = shutil.which("jq")
        jq_path = f":{to_posix(Path(jq).parent)}" if jq else ""
        env["PATH"] = f"{to_posix(tmp_path)}:/usr/bin:/bin{jq_path}"
        env["DIAGNOSTICS_PATH"] = to_posix(diagnostics)
        env["EVIDENCE_PATH"] = to_posix(evidence)
        command = [bash, to_posix(SCRIPT)]
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_findings_use_exit_183_and_only_safe_fields_reach_summary(tmp_path: Path) -> None:
    result = run_scan(
        tmp_path,
        fake_docker="""#!/usr/bin/env bash
printf '%s\n' '{"DetectorName":"GitHub Token","Raw":"raw-secret-value","SourceMetadata":{"Data":{"Filesystem":{"file":"/evidence/replay.json"}}}}'
exit 183
""",
    )

    summary = (tmp_path / "diagnostics" / "summary.txt").read_text(encoding="utf-8")
    assert result.returncode == 1
    assert "scan_status=findings" in summary
    assert "scanner_exit_code=183" in summary
    assert "finding=GitHub Token path=replay.json" in summary
    assert "raw-secret-value" not in summary
    assert "scanner_attempts=1" in summary


def test_infrastructure_failure_retries_once_and_keeps_only_redacted_tail(tmp_path: Path) -> None:
    result = run_scan(
        tmp_path,
        fake_docker="""#!/usr/bin/env bash
printf '%s\n' 'network token=super-secret-value' >&2
exit 125
""",
    )

    summary = (tmp_path / "diagnostics" / "summary.txt").read_text(encoding="utf-8")
    assert result.returncode == 1
    assert "scan_status=infra_error" in summary
    assert "scanner_exit_code=125" in summary
    assert "scanner_attempts=2" in summary
    assert "token=[REDACTED]" in summary
    assert "super-secret-value" not in summary
    assert "scanner_stderr_tail_begin" in summary
    assert "scanner_stderr_tail_end" in summary
