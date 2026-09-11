#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
exec "${PRISM:-prism}" build . -o .build/ledger
