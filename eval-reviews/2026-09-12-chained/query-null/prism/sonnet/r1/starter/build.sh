#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
prism main.pr -o .build/query-null
