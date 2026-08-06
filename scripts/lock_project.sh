#!/bin/bash
# Regenerate pyproject.toml + uv.lock from compiler.properties.
#
# Run after bumping COM_VERSION, then commit both files. pyproject.toml cannot
# interpolate COM_VERSION, so it is a generated artifact rather than a source.
set -euo pipefail
SCRIPT_DIR=$(realpath "$(dirname "$0")")
PROJECT_ROOT=$(realpath "${SCRIPT_DIR}/..")
source "${SCRIPT_DIR}/uv_util.sh"
source "${PROJECT_ROOT}/compiler.properties"

resolve_pip_cmd 1
if [ "${PIP_CMD}" != "uv pip" ]; then
    echo "ERROR: uv is required to regenerate uv.lock." >&2
    exit 1
fi

if [ -z "${COM_VERSION:-}" ]; then
    echo "ERROR: COM_VERSION not set in compiler.properties." >&2
    exit 1
fi

PYPROJECT="${PROJECT_ROOT}/pyproject.toml"
echo "Rewriting ${PYPROJECT} for dx-com ${COM_VERSION}..."

# Rewrite only the two COM_VERSION-derived lines; everything else is stable.
sed -i \
    -e "s|^    \"dx-com==.*\",$|    \"dx-com==${COM_VERSION}\",|" \
    -e "s|^find-links = \[\".*\"\]$|find-links = [\"https://sdk.deepx.ai/release/dxcom/v${COM_VERSION}/index.html\"]|" \
    "${PYPROJECT}"

grep -q "dx-com==${COM_VERSION}" "${PYPROJECT}" || {
    echo "ERROR: failed to rewrite dx-com pin in ${PYPROJECT}." >&2
    exit 1
}
grep -q "v${COM_VERSION}/index.html" "${PYPROJECT}" || {
    echo "ERROR: failed to rewrite find-links in ${PYPROJECT}." >&2
    exit 1
}

(cd "${PROJECT_ROOT}" && uv lock)
echo "Done. Review and commit pyproject.toml and uv.lock"
