#!/usr/bin/env bash
# Thin forwarder to the canonical runner at the repository root.
#
# Kept because the Herald `mediasync` service invokes this exact path.
# All logic — env validation, venv checks, PYTHONPATH, logging — lives in
# ../run_mediasync.sh so the two entry points cannot drift apart again.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$(cd "$SCRIPT_DIR/.." && pwd)/run_mediasync.sh" "$@"
