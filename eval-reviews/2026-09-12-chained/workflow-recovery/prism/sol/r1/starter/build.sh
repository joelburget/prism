#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
"${PRISM:-prism}" main.pr -o .build/workflow
