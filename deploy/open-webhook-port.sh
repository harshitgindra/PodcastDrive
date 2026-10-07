#!/bin/bash
# Public ingress for the webhook is intentionally disabled.
# Access it through an SSM port forward or another authenticated private tunnel.
set -euo pipefail
printf '%s\n' \
  'Refusing to open TCP/9090 to the internet.' \
  'The webhook is loopback-only and has no built-in TLS.' \
  'Use SSM port forwarding or an authenticated private access path instead.' >&2
exit 1
