#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
# link.sh bundles the per-function objects into one archive before linking so
# the build succeeds under a small open-file limit; see its header comment.
PRISM_REAL_CC="${PRISM_CC:-}" PRISM_CC="$(pwd)/link.sh" prism main.pr -o .build/query-null
