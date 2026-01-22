#!/usr/bin/env bash
set -euo pipefail

if find /usr/local/share/ca-certificates/custom -maxdepth 1 -name '*.crt' -print -quit | \
  grep -q .; then
  update-ca-certificates
fi

if [ "$#" -eq 0 ]; then
  exit 0
fi

exec "$@"
