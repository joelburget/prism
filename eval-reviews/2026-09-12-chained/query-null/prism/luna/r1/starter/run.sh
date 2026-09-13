#!/bin/sh
set -eu
exec prism run "$(dirname "$0")/main.pr"
