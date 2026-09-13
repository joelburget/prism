#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p .build
"${PRISM:-prism}" check main.pr
printf '%s\n' '#!/bin/sh' 'exec prism run main.pr | sed '\''/^=> ()$/d'\''' > .build/workflow
chmod +x .build/workflow
