#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
if "${PRISM:-prism}" main.pr -o .build/workflow; then
  exit 0
fi
# Fallback for sandboxes whose open-file limit is below the number of object
# chunks the linker receives: bundle those chunks into one archive with a shim.
mkdir -p .build/shim
cat > .build/shim/ld <<'SHIM'
#!/bin/sh
set -eu
bundle="$(dirname "$0")/chunks.a"
rm -f "$bundle"
rest=""
chunks=""
bundled=0
for arg in "$@"; do
  case "$arg" in
    *prism_rt.d/*.o)
      chunks="$chunks $arg"
      if [ "$bundled" -eq 0 ]; then
        rest="$rest --whole-archive $bundle --no-whole-archive"
        bundled=1
      fi
      ;;
    *) rest="$rest $arg" ;;
  esac
done
if [ "$bundled" -eq 0 ]; then
  exec /usr/bin/ld "$@"
fi
ar rcs "$bundle" $chunks
exec /usr/bin/ld $rest
SHIM
chmod +x .build/shim/ld
PATH="$(pwd)/.build/shim:$PATH"
export PATH
exec "${PRISM:-prism}" main.pr -o .build/workflow
