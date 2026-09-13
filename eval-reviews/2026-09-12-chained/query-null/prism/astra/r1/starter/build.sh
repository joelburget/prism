#!/bin/sh
set -eu
cd "$(dirname "$0")"
ulimit -n 4096
mkdir -p .build
prism main.pr -o .build/query-null
