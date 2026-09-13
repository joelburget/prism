#!/bin/sh
# Extra edge-case traces beyond the public suite. Each case: request | expected response.
set -eu
cd "$(dirname "$0")"
fail=0
check() {
  name=$1; input=$2; expected=$3
  actual=$(printf '%s\n' "$input" | ./run.sh)
  if python3 -c 'import json,sys; a,b=sys.argv[1:]; sys.exit(0 if json.loads(a)==json.loads(b) else 1)' "$actual" "$expected"; then
    echo "PASS $name"
  else
    echo "FAIL $name"; echo "  expected: $expected"; echo "  actual:   $actual"; fail=1
  fi
}
S='{"id":"a","needs":[],"amount":5}'
# Success lost before commit is replayed with no second effect.
check replayed-after-crash \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"commands":[{"op":"start","run":"r"},{"op":"tick","crash_at":"after_call"},{"op":"restart"},{"op":"tick"}]}}' \
  '{"ok":true,"result":{"observations":[],"final":{"now":0,"up":true,"runs":[{"id":"r","status":"succeeded","cancel_requested":false,"steps":[{"id":"a","status":"succeeded","attempts":1,"ready_at":0}]}],"calls":[{"kind":"execute","key":["r","a"],"attempt":1,"outcome":"applied"},{"kind":"execute","key":["r","a"],"attempt":1,"outcome":"replayed"}],"effects":[{"key":["r","a"],"amount":5}]}}}'
# Cancelling a cancelling run again is harmless; the lookup still reconciles.
check repeated-cancel-while-cancelling \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"commands":[{"op":"start","run":"r"},{"op":"tick","crash_at":"after_begin"},{"op":"restart"},{"op":"cancel","run":"r"},{"op":"cancel","run":"r"},{"op":"tick"},{"op":"cancel","run":"r"}]}}' \
  '{"ok":true,"result":{"observations":[],"final":{"now":0,"up":true,"runs":[{"id":"r","status":"cancelled","cancel_requested":true,"steps":[{"id":"a","status":"cancelled","attempts":1,"ready_at":0}]}],"calls":[{"kind":"lookup","key":["r","a"],"attempt":1,"outcome":"missing"}],"effects":[]}}}'
# Cancelling a failed run leaves cancel_requested false.
check cancel-failed-run-noop \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"max_attempts":1,"steps":[{"id":"a","needs":[],"amount":5,"failures":1}],"commands":[{"op":"start","run":"r"},{"op":"tick"},{"op":"cancel","run":"r"},{"op":"tick"}]}}' \
  '{"ok":true,"result":{"observations":[],"final":{"now":0,"up":true,"runs":[{"id":"r","status":"failed","cancel_requested":false,"steps":[{"id":"a","status":"failed","attempts":1,"ready_at":0}]}],"calls":[{"kind":"execute","key":["r","a"],"attempt":1,"outcome":"transient"}],"effects":[]}}}'
# Crash while down and restart while up are errors with no partial result.
check crash-while-down \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"commands":[{"op":"crash"},{"op":"crash"}]}}' \
  '{"ok":false,"error":{"code":"PROCESS_DOWN"}}'
check start-while-down \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"commands":[{"op":"crash"},{"op":"start","run":"r"}]}}' \
  '{"ok":false,"error":{"code":"PROCESS_DOWN"}}'
# Failure counters are per run: a second run sees its own transient.
check failures-per-run \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":[{"id":"a","needs":[],"amount":5,"failures":1}],"commands":[{"op":"start","run":"r1"},{"op":"tick"},{"op":"advance","by":2},{"op":"tick"},{"op":"start","run":"r2"},{"op":"tick"}]}}' \
  '{"ok":true,"result":{"observations":[],"final":{"now":2,"up":true,"runs":[{"id":"r1","status":"succeeded","cancel_requested":false,"steps":[{"id":"a","status":"succeeded","attempts":2,"ready_at":2}]},{"id":"r2","status":"active","cancel_requested":false,"steps":[{"id":"a","status":"pending","attempts":1,"ready_at":4}]}],"calls":[{"kind":"execute","key":["r1","a"],"attempt":1,"outcome":"transient"},{"kind":"execute","key":["r1","a"],"attempt":2,"outcome":"applied"},{"kind":"execute","key":["r2","a"],"attempt":1,"outcome":"transient"}],"effects":[{"key":["r1","a"],"amount":5}]}}}'
# Boolean crash_at and non-integer numbers are INVALID_INPUT.
check boolean-crash-at \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"commands":[{"op":"tick","crash_at":true}]}}' \
  '{"ok":false,"error":{"code":"INVALID_INPUT"}}'
check float-amount \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":[{"id":"a","needs":[],"amount":5.0}],"commands":[]}}' \
  '{"ok":false,"error":{"code":"INVALID_INPUT"}}'
exit $fail
