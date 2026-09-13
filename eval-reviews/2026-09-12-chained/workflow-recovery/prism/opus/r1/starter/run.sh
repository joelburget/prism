#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .build/workflow ]; then
  echo 'Run ./build.sh once before running the adapter.' >&2
  exit 1
fi
exec .build/workflow
