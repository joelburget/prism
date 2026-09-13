#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
# cc.sh wraps the real C compiler (PRISM_CC or clang) to link without ThinLTO; see its header.
PRISM_REAL_CC="${PRISM_CC:-/usr/lib/llvm-22/bin/clang}" PRISM_CC="$(pwd)/cc.sh" \
  "${PRISM:-prism}" main.pr -o .build/workflow
