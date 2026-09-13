#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
build_target=".build/query-null.$$"
trap 'rm -f "$build_target"' EXIT HUP INT TERM
prism --query-threads 1 main.pr -o "$build_target"
mv -f "$build_target" .build/query-null
trap - EXIT HUP INT TERM
