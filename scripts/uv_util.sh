#!/bin/bash
# uv (Astral) detection and bootstrap for dx-compiler install.sh.
#
# Exports two variables consumed by install.sh:
#   PIP_CMD            "pip"              | "uv pip"
#   PIP_UNINSTALL_CMD  "pip uninstall -y" | "uv pip uninstall"
#
# uv pip is CLI-compatible with pip for install/uninstall, so callers just
# swap the command prefix. Two exceptions are NOT routed through here:
#   - `pip download`: uv has no equivalent. Archive mode keeps pip.
#   - `pip uninstall`: uv never prompts, so it rejects `-y`.

# Pinned so the installer never pulls an unreviewed uv release.
UV_PIN="${UV_PIN:-0.12.2}"

# Set UV_NO_BOOTSTRAP=1 to probe only (used by tests and by offline CI).
UV_NO_BOOTSTRAP="${UV_NO_BOOTSTRAP:-0}"

uv_available() {
    command -v uv >/dev/null 2>&1
}

# Install uv into the currently active environment. Returns non-zero on
# failure so the caller can fall back to pip instead of aborting install.
bootstrap_uv() {
    if [ "${UV_NO_BOOTSTRAP}" = "1" ]; then
        return 1
    fi
    local pip_target_args=""
    # Outside a venv, install to the user site so we never touch system dirs
    # (PEP 668 blocks system-wide pip on Ubuntu 24.04+).
    if [ -z "${VIRTUAL_ENV:-}" ]; then
        pip_target_args="--user"
    fi
    pip install ${pip_target_args} "uv==${UV_PIN}" >&2 || return 1
    # `pip install --user` lands in ~/.local/bin, which may not be on PATH.
    if ! command -v uv >/dev/null 2>&1; then
        PATH="${HOME}/.local/bin:${PATH}"
        export PATH
    fi
    command -v uv >/dev/null 2>&1
}

# resolve_pip_cmd <use_uv:0|1>
# Sets PIP_CMD and PIP_UNINSTALL_CMD. Never exits; always leaves a usable
# command pair so a uv problem degrades to pip rather than failing install.
resolve_pip_cmd() {
    local use_uv="${1:-0}"

    PIP_CMD="pip"
    PIP_UNINSTALL_CMD="pip uninstall -y"

    if [ "${use_uv}" != "1" ]; then
        return 0
    fi

    if ! uv_available; then
        if ! bootstrap_uv; then
            echo "[WARNING] uv requested but unavailable and bootstrap failed; falling back to pip." >&2
            return 0
        fi
    fi

    PIP_CMD="uv pip"
    PIP_UNINSTALL_CMD="uv pip uninstall"
    return 0
}
