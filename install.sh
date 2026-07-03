#!/bin/bash
# Thin wrapper — business logic moved to install.py (behavior-preserving port).
# Kept so existing callers (docs, CI, docker_build.sh, `./install.sh --target=...`)
# keep working unchanged. The original shell orchestrator is in git history on the
# base commit of this branch; the Python port lives in install.py.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
exec python3 "${SCRIPT_DIR}/install.py" "$@"
