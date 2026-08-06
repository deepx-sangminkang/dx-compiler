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

# uv.lock must stay in sync with COM_VERSION in compiler.properties.
PROJECT_ROOT="${SCRIPT_DIR}/.."
if [ -f "${PROJECT_ROOT}/uv.lock" ]; then
    # shellcheck disable=SC1091
    . "${PROJECT_ROOT}/compiler.properties"
    if grep -q "dx-com==${COM_VERSION}" "${PROJECT_ROOT}/pyproject.toml"; then
        echo "ok: pyproject.toml pins dx-com==${COM_VERSION}"
    else
        echo "FAIL: pyproject.toml does not pin dx-com==${COM_VERSION}; run scripts/lock_project.sh"
        FAILED=1
    fi
    if grep -q "version = \"${COM_VERSION}\"" "${PROJECT_ROOT}/uv.lock"; then
        echo "ok: uv.lock contains dx-com ${COM_VERSION}"
    else
        echo "FAIL: uv.lock is stale for COM_VERSION=${COM_VERSION}; run scripts/lock_project.sh"
        FAILED=1
    fi
    # install_dx_com() decides whether to trust uv.lock by grepping it for this
    # exact version-bearing URL. If uv ever changes how it records the registry,
    # that guard would silently go always-false and the lock path would quietly
    # stop being used, so pin the assumption here.
    if grep -q "dxcom/v${COM_VERSION}/index.html" "${PROJECT_ROOT}/uv.lock"; then
        echo "ok: uv.lock carries the registry URL install.sh's staleness guard greps for"
    else
        echo "FAIL: uv.lock has no 'dxcom/v${COM_VERSION}/index.html'; install.sh would never use the lock"
        FAILED=1
    fi
    if grep -q "dxcom/v0.0.0-nonexistent/index.html" "${PROJECT_ROOT}/uv.lock"; then
        echo "FAIL: staleness guard matches a version that is not in the lock"
        FAILED=1
    else
        echo "ok: staleness guard rejects a COM_VERSION the lock was not built for"
    fi
else
    echo "ok: uv.lock absent, lock consistency check skipped"
fi

rm -rf "${_UV_FAKE_DIR}"
[ $FAILED -eq 0 ] && echo "ALL PASS" || exit 1
