#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
# Parallel LTO code generation opens more files than a constrained sandbox
# allows; retry the same build with single-threaded linking if that happens.
prism main.pr -o .build/query-null ||
  CCC_OVERRIDE_OPTIONS='+-Wl,-plugin-opt=jobs=1' \
    prism main.pr -o .build/query-null
