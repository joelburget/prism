#!/bin/sh
# C-compiler shim used by build.sh through PRISM_CC.
#
# Prism compiles one object per call-graph component and then hands all of
# them to the system linker in a single invocation. GNU ld keeps every input
# open at once, so on hosts with a small open-file limit (256 here) a program
# of this size fails to link with "Too many open files". For link steps with
# many objects this shim first bundles the objects into one archive and links
# it with --whole-archive, which needs a single descriptor. Every other
# invocation (probes, per-object compiles) is passed straight through.
set -u
if [ -n "${PRISM_REAL_CC:-}" ]; then
  CC="$PRISM_REAL_CC"
elif [ -x /usr/lib/llvm-22/bin/clang ]; then
  CC=/usr/lib/llvm-22/bin/clang
else
  CC=clang
fi
count=0
compile=0
for a in "$@"; do
  case "$a" in
    *.o) count=$((count + 1)) ;;
    -c) compile=1 ;;
  esac
done
if [ "$compile" -eq 1 ] || [ "$count" -lt 64 ] || ! command -v ar >/dev/null 2>&1; then
  exec "$CC" "$@"
fi
archive="$(mktemp -t prism_link.XXXXXX)"
rm -f "$archive"
archive="$archive.a"
objs=""
rest=""
for a in "$@"; do
  case "$a" in
    *.o) objs="$objs $a" ;;
    *) rest="$rest $a" ;;
  esac
done
if ! ar rcs "$archive" $objs; then
  rm -f "$archive"
  exec "$CC" "$@"
fi
"$CC" $rest -Wl,--whole-archive "$archive" -Wl,--no-whole-archive
status=$?
rm -f "$archive"
exit $status
