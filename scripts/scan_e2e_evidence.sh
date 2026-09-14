#!/usr/bin/env bash
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

# Scan replay evidence and publish only bounded, sanitized diagnostics.

set -euo pipefail

: "${DATABASE:?DATABASE is required}"
: "${DIAGNOSTICS_PATH:?DIAGNOSTICS_PATH is required}"
: "${EVIDENCE_PATH:?EVIDENCE_PATH is required}"
: "${SCENARIO_OUTCOME:?SCENARIO_OUTCOME is required}"
: "${TRUFFLEHOG_IMAGE:?TRUFFLEHOG_IMAGE is required}"

mkdir -p "${DIAGNOSTICS_PATH}"
summary="${DIAGNOSTICS_PATH}/summary.txt"
scanner_json="${DIAGNOSTICS_PATH}/trufflehog.jsonl"
scanner_stderr="${DIAGNOSTICS_PATH}/trufflehog.stderr.log"
{
    echo "database=${DATABASE}"
    echo "diagnostic_format=powercontext-e2e-v1"
    echo "scenario_outcome=${SCENARIO_OUTCOME}"
    echo "scanner_image=${TRUFFLEHOG_IMAGE}"
} > "${summary}"

if ! test -d "${EVIDENCE_PATH}"; then
    echo "evidence_status=missing" >> "${summary}"
    echo "Replay evidence directory was not produced." >&2
    exit 1
fi

file_count="$(find "${EVIDENCE_PATH}" -type f | wc -l | tr -d '[:space:]')"
if test "${file_count}" = 0; then
    echo "evidence_status=empty" >> "${summary}"
    echo "Replay evidence directory was empty." >&2
    exit 1
fi
{
    echo "evidence_status=present"
    echo "evidence_file_count=${file_count}"
} >> "${summary}"

run_scan() {
    local json_output="$1"
    local stderr_output="$2"
    docker run --rm \
        --network none \
        --volume "${EVIDENCE_PATH}:/evidence:ro" \
        "${TRUFFLEHOG_IMAGE}" \
        filesystem /evidence \
        --no-verification \
        --results=verified,unknown,unverified \
        --fail \
        --fail-on-scan-errors \
        --no-update \
        --json > "${json_output}" 2> "${stderr_output}" || return $?
}

if run_scan "${scanner_json}" "${scanner_stderr}"; then
    scanner_status=0
else
    scanner_status=$?
fi
scanner_attempts=1
if test "${scanner_status}" -ne 0 && test "${scanner_status}" -ne 183; then
    scanner_attempts=2
    scanner_json_retry="${DIAGNOSTICS_PATH}/trufflehog-retry.jsonl"
    scanner_stderr_retry="${DIAGNOSTICS_PATH}/trufflehog-retry.stderr.log"
    if run_scan "${scanner_json_retry}" "${scanner_stderr_retry}"; then
        scanner_status=0
    else
        scanner_status=$?
    fi
    scanner_json="${scanner_json_retry}"
    scanner_stderr="${scanner_stderr_retry}"
fi

echo "scanner_attempts=${scanner_attempts}" >> "${summary}"
if test "${scanner_status}" -eq 183; then
    echo "scan_status=findings" >> "${summary}"
    echo "scanner_exit_code=183" >> "${summary}"
    if command -v jq >/dev/null 2>&1; then
        jq -r '
            select(.DetectorName? != null)
            | [
                (.DetectorName | tostring | .[0:120]),
                ((.SourceMetadata.Data.Filesystem.file // .SourceMetadata.Data.Filesystem.path // "unknown")
                    | tostring
                    | sub("^/evidence/?"; "")
                    | .[0:240])
              ]
            | @tsv
        ' "${scanner_json}" 2>/dev/null \
            | head -n 20 \
            | sed $'s/^/finding=/; s/\t/ path=/' \
            | sed -E \
                -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
                -e 's/((token|secret|password|api[_-]?key)[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED]/Ig' \
                -e 's/[A-Za-z0-9+\/_=-]{32,}/[REDACTED]/g' >> "${summary}" || true
    else
        echo "finding_diagnostics=jq_unavailable" >> "${summary}"
    fi
    echo "Replay evidence contained scanner findings; raw evidence will not be uploaded." >&2
    exit 1
fi

if test "${scanner_status}" -ne 0; then
    echo "scan_status=infra_error" >> "${summary}"
    echo "scanner_exit_code=${scanner_status}" >> "${summary}"
    {
        echo "scanner_stderr_tail_begin"
        tail -n 50 "${scanner_stderr}" \
            | tr -cd '\11\12\15\40-\176' \
            | sed -E \
                -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED]/Ig' \
                -e 's/((token|secret|password|api[_-]?key)[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED]/Ig' \
                -e 's/[A-Za-z0-9+\/_=-]{32,}/[REDACTED]/g'
        echo "scanner_stderr_tail_end"
    } >> "${summary}"
    echo "Replay evidence scanning failed due to infrastructure error; raw evidence will not be uploaded." >&2
    exit 1
fi

echo "scan_status=clean" >> "${summary}"
