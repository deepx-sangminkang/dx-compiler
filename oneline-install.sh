#!/bin/sh
# DEEPX dx-compiler one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/DEEPX-AI/dx-compiler/main/oneline-install.sh | sh
#
# Env overrides:
#   DX_VERSION=vX.Y.Z       pin a release (default: latest release)
#   DX_INSTALL_DIR=<dir>    install root (default: ~/deepx)
#   DX_INSTALL_ARGS=<args>  extra flags passed through to install.sh
#                           (e.g. running inside a container: install.sh detects
#                           it and requires DX_INSTALL_ARGS=--docker_volume_path=<dir>)
set -eu

REPO="DEEPX-AI/dx-compiler"
INSTALL_ROOT="${DX_INSTALL_DIR:-$HOME/deepx}"

log() { printf '\033[1;34m[dx-compiler]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[dx-compiler][WARN]\033[0m %s\n' "$1" >&2; }
die() { printf '\033[1;31m[dx-compiler][ERROR]\033[0m %s\n' "$1" >&2; exit 1; }

main() {
    command -v curl >/dev/null 2>&1 || die "curl is required"
    command -v tar  >/dev/null 2>&1 || die "tar is required"
    command -v bash >/dev/null 2>&1 || die "bash is required"

    TAG="${DX_VERSION:-}"
    if [ -z "$TAG" ]; then
        log "Resolving latest release tag"
        TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
              | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
        [ -n "$TAG" ] || die "failed to resolve latest release tag (pin with DX_VERSION=vX.Y.Z)"
    fi

    # TAG is spliced into both a GitHub URL and a local path below — validate before either
    # use. Blocks '..' (path/URL traversal), a leading '/' (absolute path), and anything
    # outside [A-Za-z0-9._/-] (shell/URL metacharacters). Applied to both the pinned
    # (DX_VERSION) and API-resolved (latest release) paths — the latter is our own trusted
    # response, but validating both is cheaper than reasoning about which path is trusted.
    case "$TAG" in
        ''|*..*|/*|*[!A-Za-z0-9._/-]*) die "invalid DX_VERSION: $TAG" ;;
    esac

    [ -z "${DX_REF:-}" ] || warn "DX_REF is ignored by dx-compiler; installing $TAG (set DX_VERSION to pin a version)"

    DEST="$INSTALL_ROOT/dx-compiler-$TAG"
    log "Installing dx-compiler $TAG into $DEST"
    mkdir -p "$DEST"
    curl -fL "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" \
        | tar xz --strip-components=1 -C "$DEST" \
        || die "failed to download/extract release tarball"

    cd "$DEST"
    # ponytail: unquoted on purpose — DX_INSTALL_ARGS carries operator-supplied flags that must
    # word-split; set -f keeps that word-splitting but excludes pathname (glob) expansion, so a
    # stray */? in a flag value reaches install.sh literally instead of expanding against $DEST.
    set -f
    # shellcheck disable=SC2086
    bash ./install.sh ${DX_INSTALL_ARGS:-} || die "install.sh failed — see output above"

    log "Done. dx-compiler $TAG installed at $DEST"
}

main "$@"
