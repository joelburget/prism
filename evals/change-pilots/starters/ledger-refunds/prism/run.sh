#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .build/ledger ]; then
  echo 'Run ./build.sh once before launching the ledger.' >&2
  exit 1
fi
exec .build/ledger
