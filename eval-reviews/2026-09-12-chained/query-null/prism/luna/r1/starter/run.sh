#!/bin/sh
set -eu
exec "$(dirname "$0")/.build/query-null"
