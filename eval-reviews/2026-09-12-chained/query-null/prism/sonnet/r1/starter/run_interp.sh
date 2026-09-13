#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec prism run main.pr 2>/dev/null
