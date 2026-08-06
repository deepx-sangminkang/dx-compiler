#!/bin/bash
# Self-check for scripts/uv_util.sh. No framework: plain asserts, exit 1 on failure.
set -u
SCRIPT_DIR=$(realpath "$(dirname "$0")")
source "${SCRIPT_DIR}/../scripts/uv_util.sh"

FAILED=0
assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" != "$actual" ]; then
        echo "FAIL: ${msg}: expected '${expected}', got '${actual}'"
        FAILED=1
    else
        echo "ok: ${msg}"
    fi
}

# USE_UV=0 -> plain pip, no uv probing at all
resolve_pip_cmd 0
assert_eq "pip" "$PIP_CMD" "USE_UV=0 keeps PIP_CMD=pip"
assert_eq "pip uninstall -y" "$PIP_UNINSTALL_CMD" "USE_UV=0 keeps pip uninstall -y"

# USE_UV=1 with uv present on PATH -> uv pip
_UV_FAKE_DIR=$(mktemp -d)
printf '#!/bin/sh\necho "uv 0.0.0-fake"\n' > "${_UV_FAKE_DIR}/uv"
chmod +x "${_UV_FAKE_DIR}/uv"
PATH="${_UV_FAKE_DIR}:${PATH}" resolve_pip_cmd 1
assert_eq "uv pip" "$PIP_CMD" "USE_UV=1 with uv present selects uv pip"
assert_eq "uv pip uninstall" "$PIP_UNINSTALL_CMD" "uv pip uninstall takes no -y"

# USE_UV=1 but bootstrap disabled and uv absent -> must fall back to pip, not fail
PATH="/usr/bin:/bin" UV_NO_BOOTSTRAP=1 resolve_pip_cmd 1
assert_eq "pip" "$PIP_CMD" "uv absent + no bootstrap falls back to pip"

# install.sh must advertise and accept --uv, and reject bad values.
INSTALL_SH="${SCRIPT_DIR}/../install.sh"

HELP_OUT=$("${INSTALL_SH}" --help 2>&1)
case "$HELP_OUT" in
    *"--uv="*) echo "ok: install.sh --help documents --uv" ;;
    *) echo "FAIL: install.sh --help does not document --uv"; FAILED=1 ;;
esac

BAD_OUT=$("${INSTALL_SH}" --uv=maybe 2>&1)
BAD_RC=$?
if [ "$BAD_RC" -eq 0 ]; then
    echo "FAIL: --uv=maybe should exit non-zero"
    FAILED=1
else
    echo "ok: --uv=maybe rejected (exit ${BAD_RC})"
fi
case "$BAD_OUT" in
    *"Invalid value for --uv"*) echo "ok: --uv=maybe error message is specific" ;;
    *) echo "FAIL: --uv=maybe error message missing"; FAILED=1 ;;
esac

rm -rf "${_UV_FAKE_DIR}"
[ $FAILED -eq 0 ] && echo "ALL PASS" || exit 1
