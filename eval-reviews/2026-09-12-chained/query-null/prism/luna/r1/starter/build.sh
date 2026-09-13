#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
prism check main.pr
