#!/bin/sh
# Prism links its generated runtime as many small objects.  The task container's
# descriptor ceiling is lower than that object count, so coalesce object groups
# with relocatable links before the final native link.
set -eu
for arg in "$@"; do
  if [ "$arg" = "-c" ]; then
    exec /usr/lib/llvm-22/bin/clang "$@"
  fi
done

link_tmp=$(mktemp -d)
trap 'rm -rf "$link_tmp"' EXIT HUP INT TERM
other_args=""
object_args=""
object_count=0
chunk_count=0
chunks=""

flush_objects() {
  if [ "$object_count" -gt 0 ]; then
    chunk="$link_tmp/chunk-$chunk_count.bc"
    # Generated paths contain no whitespace (the build directory is fixed).
    /usr/lib/llvm-22/bin/llvm-link $object_args -o "$chunk"
    chunks="$chunks $chunk"
    chunk_count=$((chunk_count + 1))
    object_args=""
    object_count=0
  fi
}

for arg in "$@"; do
  case "$arg" in
    *.o)
      object_args="$object_args $arg"
      object_count=$((object_count + 1))
      if [ "$object_count" -ge 80 ]; then flush_objects; fi
      ;;
    *) other_args="$other_args $arg" ;;
  esac
done
flush_objects
# shellcheck disable=SC2086
/usr/lib/llvm-22/bin/clang $chunks $other_args
