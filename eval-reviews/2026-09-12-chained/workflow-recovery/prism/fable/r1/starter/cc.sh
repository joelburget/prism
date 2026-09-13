#!/bin/sh
# C-compiler shim for the Prism backend. Prism compiles one object per function group
# and links them with ThinLTO; GNU ld's LTO plugin then needs a temporary file per
# object, which exceeds the 256 open-file cap of the task container for a program of
# this size. Dropping `-flto=thin` links the same objects natively with identical
# behavior. Every other argument is forwarded untouched.
real="${PRISM_REAL_CC:-/usr/lib/llvm-22/bin/clang}"
set --  "$@" --end-of-shim--
while [ "$1" != "--end-of-shim--" ]; do
  a="$1"; shift
  [ "$a" = "-flto=thin" ] || set -- "$@" "$a"
done
shift
exec "$real" "$@"
