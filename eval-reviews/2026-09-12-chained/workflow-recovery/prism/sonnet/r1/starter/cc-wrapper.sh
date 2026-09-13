#!/bin/sh
# Serializes ThinLTO backend codegen (-flto-jobs=1). The default parallel
# codegen opens one temp object per partition; sandboxes with a low open-file
# ulimit can exhaust it once the program has enough modules/functions.
cc="${PRISM_REAL_CC:-}"
if [ -z "$cc" ]; then
  for candidate in clang /usr/lib/llvm-22/bin/clang /usr/local/opt/llvm/bin/clang cc gcc; do
    if command -v "$candidate" >/dev/null 2>&1; then
      cc="$candidate"
      break
    fi
  done
fi
exec "$cc" -flto-jobs=1 "$@"
