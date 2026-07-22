#!/usr/bin/env bash
#
# Capture real Tailscale CLI outputs into $TAILCTL_HOME/fixtures/ for use by
# `tailctl init` and `tailctl doctor`. These files are private to your machine
# and are not committed to the repo. For the sanitized test fixtures see
# tests/fixtures/ instead.
#
# Usage:
#   bash scripts/capture-fixtures.sh
#
# Overrides:
#   TAILCTL_HOME   defaults to ~/.tailctl
#   TAILSCALE_BIN  defaults to /Applications/Tailscale.app/Contents/MacOS/Tailscale

set -euo pipefail

TAILCTL_HOME="${TAILCTL_HOME:-$HOME/.tailctl}"
TAILSCALE_BIN="${TAILSCALE_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"
DEST="$TAILCTL_HOME/fixtures"

if [[ ! -x "$TAILSCALE_BIN" ]]; then
    echo "error: tailscale binary not found at $TAILSCALE_BIN" >&2
    echo "set TAILSCALE_BIN=/path/to/tailscale and retry" >&2
    exit 1
fi

mkdir -p "$DEST"

echo ">>> capturing switch-list.json"
"$TAILSCALE_BIN" switch --list --json > "$DEST/switch-list.json"

echo ">>> capturing status-ready.json"
"$TAILSCALE_BIN" status --json > "$DEST/status-ready.json"

echo
echo "captured fixtures into $DEST"
ls -la "$DEST"
