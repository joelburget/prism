#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build

# A constrained sandbox may allow fewer open file descriptors than the LTO
# link wants: it hands the linker every bitcode object at once. This wrapper
# bundles those objects into one archive, which the linker reads through a
# single descriptor, and is only used if a plain link fails.
cat > .build/cc-bundle.sh <<'WRAP'
#!/bin/sh
real="${PRISM_REAL_CC:-}"
if [ -z "$real" ]; then
  for c in /usr/lib/llvm-22/bin/clang clang cc; do
    command -v "$c" >/dev/null 2>&1 && { real="$c"; break; }
  done
fi
archiver=""
for a in llvm-ar /usr/lib/llvm-22/bin/llvm-ar ar; do
  command -v "$a" >/dev/null 2>&1 && { archiver="$a"; break; }
done
objs=""
rest=""
n=0
for arg in "$@"; do
  case "$arg" in
    *.o) objs="$objs $arg"; n=$((n + 1));;
    *) rest="$rest $arg";;
  esac
done
if [ "$n" -gt 32 ] && [ -n "$archiver" ]; then
  dir=$(dirname "$(echo $objs | cut -d' ' -f1)")
  rm -f "$dir/bundle.a"
  if "$archiver" rcs "$dir/bundle.a" $objs; then
    exec "$real" $rest -Wl,--whole-archive "$dir/bundle.a" -Wl,--no-whole-archive
  fi
fi
exec "$real" "$@"
WRAP
chmod +x .build/cc-bundle.sh

prism main.pr -o .build/query-null ||
  CCC_OVERRIDE_OPTIONS='+-Wl,-plugin-opt=jobs=1' \
    prism main.pr -o .build/query-null ||
  PRISM_CC="$(pwd)/.build/cc-bundle.sh" prism main.pr -o .build/query-null
