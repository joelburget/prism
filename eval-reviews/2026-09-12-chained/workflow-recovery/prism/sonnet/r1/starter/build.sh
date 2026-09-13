#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
export PRISM_CC="$(pwd)/cc-wrapper.sh"
"${PRISM:-prism}" main.pr -o .build/workflow
