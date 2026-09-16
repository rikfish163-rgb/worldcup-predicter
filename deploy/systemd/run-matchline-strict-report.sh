#!/usr/bin/env bash
set -euo pipefail

# Keep compile artifacts out of the repository even when this wrapper is
# invoked manually outside the systemd environment. The cache prefix is
# disposable and lives on tmpfs; the strict report itself remains on the
# declared runtime root.
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/dev/shm/matchline-python-cache}"

# Generate the mutable runtime pointer atomically, then retain every distinct
# report as an immutable, content-addressed artifact. Historical evidence in
# the repository is read-only input and is never overwritten by this lane.
umask 027

repository_root="/home/hetaisheng/soccerdata"
runtime_dir="${MATCHLINE_RUNTIME_DIR:-/dev/shm/matchline-live-runtime}"
reports_dir="${runtime_dir}/strict-reports"
pointer="${runtime_dir}/strict-backtest-current.json"
cache_state="${runtime_dir}/strict-report-cache.json"
history_runtime_root="${MATCHLINE_HISTORY_RUNTIME_ROOT:-${runtime_dir}}"
lock_path="${MATCHLINE_STRICT_REPORT_LOCK:-${history_runtime_root}/strict-report.lock}"
history_receipt="${MATCHLINE_OPENFOOTBALL_HISTORY_RECEIPT:-${history_runtime_root}/openfootball-history-refresh-current.json}"
raw_archive_dir="${MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR:-${history_runtime_root}/openfootball-raw}"
prospective_lock="${MATCHLINE_PROSPECTIVE_LOCK:-${repository_root}/docs/evidence/prospective-model-lock-current.json}"
history_pointer="${history_runtime_root}/strict-backtest-current.json"
python_executable="${repository_root}/.venv/bin/python"

case "${runtime_dir}" in
  ""|/|/home|/home/hetaisheng|"${repository_root}")
    echo "refusing unsafe MATCHLINE_RUNTIME_DIR: ${runtime_dir}" >&2
    exit 2
    ;;
esac

case "${history_runtime_root}" in
  ""|/|/home|/home/hetaisheng|"${repository_root}")
    echo "refusing unsafe MATCHLINE_HISTORY_RUNTIME_ROOT: ${history_runtime_root}" >&2
    exit 2
    ;;
esac

if [[ "${history_runtime_root}" != /* || "${runtime_dir}" != /* ]]; then
  echo "runtime roots must be absolute" >&2
  exit 2
fi

if [ ! -x "${python_executable}" ]; then
  echo "project virtualenv Python is unavailable: ${python_executable}" >&2
  exit 2
fi
if [ -z "${raw_archive_dir}" ]; then
  echo "MATCHLINE_OPENFOOTBALL_RAW_ARCHIVE_DIR is required" >&2
  exit 2
fi

mkdir -p -- "${reports_dir}"

# The strict replay is intentionally heavier than the 15-minute current-feed
# poll. A normal and a runtime-only unit can become eligible at the same time;
# serialize them before either one opens the shared durable receipt/report.
exec 9>"${lock_path}"
if ! flock -n 9; then
  printf '{"status":"skipped_locked","lock":"%s"}\n' "${lock_path}"
  exit 0
fi

# A verified report is expensive to rebuild. Before replaying all historical
# rows, accept the existing runtime pointer only when it is content-addressed
# and its lock/history identity still matches the durable receipt. Any parse,
# hash, lock or cutoff mismatch falls through to the full verifier below.
if [[ -s "${pointer}" && -s "${history_receipt}" && -s "${prospective_lock}" ]]; then
  if "${python_executable}" - "${pointer}" "${history_receipt}" "${prospective_lock}" "${reports_dir}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

pointer_path, receipt_path, lock_path, reports_dir = map(Path, sys.argv[1:])
try:
    if pointer_path.is_symlink():
        raise ValueError("strict report pointer/archive must be regular files")
    pointer_bytes = pointer_path.read_bytes()
    report_sha = hashlib.sha256(pointer_bytes).hexdigest()
    archive = reports_dir / f"strict-backtest-{report_sha}.json"
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("strict report archive is not a regular file")
    if archive.read_bytes() != pointer_bytes:
        raise ValueError("strict report archive mismatch")
    report = json.loads(pointer_bytes)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != "matchline.strict_report.v260":
        raise ValueError("strict report schema mismatch")
    if report.get("report_lane") != "formal_v260":
        raise ValueError("strict report lane mismatch")
    report_lock = report.get("model_selection_audit", {}).get("prospective_lock", {})
    if report_lock.get("model_version_sha256") != lock.get("model_version_sha256"):
        raise ValueError("strict report model lock mismatch")
    for lock_key in ("locked_at", "evaluation_window_started_at"):
        if report_lock.get(lock_key) != lock.get(lock_key):
            raise ValueError(f"strict report lock field mismatch: {lock_key}")
    contract = report.get("training_source_contract", {})
    admission = report.get("training_admission", {})
    if contract.get("observed_before") != receipt.get("observed_at"):
        raise ValueError("strict report history cutoff mismatch")
    if contract.get("source_ids") != receipt.get("source_ids"):
        raise ValueError("strict report history source set mismatch")
    for report_key, receipt_key in (
        ("raw_admission_sha256", "admission_sha256"),
        ("raw_rows_sha256", "rows_sha256"),
        ("source_manifest_sha256", "source_manifest_sha256"),
        ("parser_contract_sha256", "parser_contract_sha256"),
    ):
        if admission.get(report_key) != receipt.get(receipt_key):
            raise ValueError(f"strict report history identity mismatch: {report_key}")
except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
    raise SystemExit(1)
PY
  then
    printf '{"status":"skipped_unchanged","pointer":"%s"}\n' "${pointer}"
    exit 0
  fi
fi

"${python_executable}" -m league_platform.runtime_evidence \
  --strict-report-from-openfootball-history \
  --runtime-dir "${history_runtime_root}" \
  --openfootball-history-receipt "${history_receipt}" \
  --openfootball-raw-archive "${raw_archive_dir}" \
  --strict-report-output "${history_runtime_root}/strict-backtest-current.json" \
  --strict-report-cache "${history_runtime_root}/strict-report-cache.json" \
  --candidate-evidence-dir "${repository_root}/docs/evidence" \
  --strict-report-prospective-lock "${prospective_lock}"

# A runtime-only cycle may keep its volatile read-model pointer on tmpfs while
# the verified history/report input lives on a durable root. Mirror only the
# bounded report pointer after the verifier succeeds; never mirror raw data or
# a receipt, and never let a failed verification replace the previous pointer.
if [[ "${history_pointer}" != "${pointer}" ]]; then
  temporary_pointer="$(mktemp --tmpdir="${runtime_dir}" ".${pointer##*/}.mirror.XXXXXX.tmp")"
  cleanup_pointer() {
    if [[ -n "${temporary_pointer:-}" && -e "${temporary_pointer}" ]]; then
      rm -f -- "${temporary_pointer}"
    fi
  }
  trap cleanup_pointer EXIT
  install -m 0640 -- "${history_pointer}" "${temporary_pointer}"
  mv -f -- "${temporary_pointer}" "${pointer}"
  temporary_pointer=""
  trap - EXIT
fi

test -s "${pointer}"
report_sha256="$(sha256sum -- "${pointer}" | awk '{print $1}')"
case "${report_sha256}" in
  ''|*[!0-9a-f]*)
    echo "invalid strict report sha256: ${report_sha256}" >&2
    exit 3
    ;;
esac
if [ "${#report_sha256}" -ne 64 ]; then
  echo "invalid strict report sha256 length" >&2
  exit 3
fi

archive="${reports_dir}/strict-backtest-${report_sha256}.json"
if [ -e "${archive}" ]; then
  cmp -s -- "${pointer}" "${archive}" || {
    echo "content-addressed strict report collision: ${archive}" >&2
    exit 4
  }
else
  temporary="$(mktemp --tmpdir="${reports_dir}" ".strict-backtest-${report_sha256}.XXXXXX.tmp")"
  cleanup() {
    if [ -n "${temporary:-}" ] && [ -e "${temporary}" ]; then
      rm -f -- "${temporary}"
    fi
  }
  trap cleanup EXIT
  install -m 0640 -- "${pointer}" "${temporary}"
  archived_sha256="$(sha256sum -- "${temporary}" | awk '{print $1}')"
  if [ "${archived_sha256}" != "${report_sha256}" ]; then
    echo "strict report changed while archiving" >&2
    exit 5
  fi
  mv -n -- "${temporary}" "${archive}"
  if [ -e "${temporary}" ]; then
    cmp -s -- "${temporary}" "${archive}" || {
      echo "content-addressed strict report collision: ${archive}" >&2
      exit 4
    }
    rm -f -- "${temporary}"
  fi
  trap - EXIT
fi

printf '{"status":"archived","sha256":"%s","artifact":"%s"}\n' \
  "${report_sha256}" "${archive}"
