#!/bin/bash
# Measure dx-compiler install wall-clock: pip path vs uv path.
#
# Each run creates a throwaway venv so neither run warms the other's cache.
# Usage: ./tests/bench_install.sh [python_version]
#   e.g. ./tests/bench_install.sh 3.11
set -uo pipefail
SCRIPT_DIR=$(realpath "$(dirname "$0")")
PROJECT_ROOT=$(realpath "${SCRIPT_DIR}/..")
PY_VER="${1:-3.11}"
RESULTS="${PROJECT_ROOT}/dx-agent-dev/bench-$(date +%Y%m%d-%H%M%S)"
mkdir -p "${RESULTS}"

# install.sh's check_virtualenv() keys purely on $VIRTUAL_ENV: when any venv is
# already active it treats that one as the environment and never activates the
# venv it just created at --venv_path. Benchmarking from inside a venv would
# therefore install into the caller's environment instead of the target one --
# and, because --force uninstalls dx-com first, would strip dx-com out of it.
# Run each case with the caller's venv removed from the environment and PATH.
BENCH_ENV=(env -u VIRTUAL_ENV)
if [ -n "${VIRTUAL_ENV:-}" ]; then
    echo "Caller venv detected (${VIRTUAL_ENV}); removing it from the benchmark environment."
    CLEAN_PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vFx "${VIRTUAL_ENV}/bin" | paste -sd: -)
    BENCH_ENV+=("PATH=${CLEAN_PATH}")
fi

# A dx-com venv is ~5.4 GB once torch and the CUDA wheels land in it. On hosts
# where /tmp is a tmpfs that is RAM, not disk -- point TMPDIR at real storage.
BENCH_TMPDIR="${TMPDIR:-/tmp}"

# The uv cache has to be purgeable for the comparison to mean anything, so
# resolve the binary up front instead of hoping it is on PATH at purge time.
UV_BIN="$(command -v uv || true)"
if [ -z "${UV_BIN}" ] && [ -x "${UV_BOOTSTRAP_DIR:-${HOME}/.local/bin}/uv" ]; then
    UV_BIN="${UV_BOOTSTRAP_DIR:-${HOME}/.local/bin}/uv"
fi
if [ -z "${UV_BIN}" ]; then
    echo "ERROR: uv not found. The uv cases would run against a cache this script" >&2
    echo "       cannot clear, making them look far faster than they are." >&2
    echo "       Install uv, or set UV_BOOTSTRAP_DIR to where it lives." >&2
    exit 1
fi
echo "Using uv at: ${UV_BIN}"

run_case() {
    local label="$1"; shift
    local venv="${BENCH_TMPDIR}/bench-${label}-$$"
    rm -rf "${venv}"
    echo "=== ${label} ===" | tee -a "${RESULTS}/bench.log"
    # Purge both download caches so every case starts cold. Without this the
    # case that runs second wins on a warm cache, and the numbers say nothing
    # about the Docker/CI cold-build case this work exists to speed up.
    # A purge that fails has to be loud: silently skipping the uv cache while
    # clearing pip's makes uv look ~20x faster than it is.
    pip cache purge >/dev/null 2>&1 || echo "WARNING: pip cache purge failed; ${label} ran warm"
    "${UV_BIN}" cache clean >/dev/null 2>&1 || echo "WARNING: uv cache clean failed; ${label} ran warm"
    local start end
    start=$(date +%s)
    (cd "${PROJECT_ROOT}" && "${BENCH_ENV[@]}" ./install.sh --target=dx_com \
        --python_version="${PY_VER}" --venv_path="${venv}" \
        "$@") >>"${RESULTS}/${label}.log" 2>&1
    local rc=$?
    end=$(date +%s)
    local elapsed=$(( end - start ))
    echo "${label},${PY_VER},${elapsed},${rc}" | tee -a "${RESULTS}/bench.csv"
    rm -rf "${venv}"
    return $rc
}

# install.sh ends by downloading sample models and the calibration dataset,
# and those scripts skip when the files are already present. Left unwarmed,
# only the first case would pay that download and the comparison would measure
# bandwidth rather than the package manager. Fetch them once up front.
echo "Pre-warming sample data so no single case pays for it..."
"${PROJECT_ROOT}/example/1-download_sample_models.sh" >"${RESULTS}/prewarm.log" 2>&1 || true
"${PROJECT_ROOT}/example/2-download_sample_calibration_dataset.sh" >>"${RESULTS}/prewarm.log" 2>&1 || true

echo "case,python,seconds,exit_code" > "${RESULTS}/bench.csv"
# Baseline and uv path both on the PyPI index, so the only variable is pip vs uv.
run_case "pip"       --uv=false --pypi=true
run_case "uv"        --uv=true  --pypi=true
# Locked path: uv.lock, no resolution at install time. Expected fastest.
run_case "uv-locked" --uv=true  --pypi=false

echo ""
echo "Results: ${RESULTS}/bench.csv"
cat "${RESULTS}/bench.csv"
