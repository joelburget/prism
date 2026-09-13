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
# --- Checkpoint two: leased workers (selected by a `workers` field) ---
W='"workers":["a","b"]'
# Spec example: expiry lets a replacement replay under the same attempt; the stale ticket cannot commit.
check leased-expired-replay \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"start","run":"r"},{"op":"claim","worker":"a"},{"op":"call","worker":"a","ticket":1},{"op":"advance","by":5},{"op":"claim","worker":"b"},{"op":"deliver","ticket":1},{"op":"call","worker":"b","ticket":2},{"op":"deliver","ticket":2}]}}' \
  '{"ok":true,"result":{"results":[null,{"ticket":1},{"kind":"execute","outcome":"applied"},null,{"ticket":2},{"committed":false},{"kind":"execute","outcome":"replayed"},{"committed":true}],"final":{"now":5,"workers":[{"id":"a","up":true},{"id":"b","up":true}],"runs":[{"id":"r","status":"succeeded","cancel_requested":false,"steps":[{"id":"a","status":"succeeded","attempts":1,"ready_at":0,"lease":null}]}],"calls":[{"worker":"a","ticket":1,"kind":"execute","key":["r","a"],"attempt":1,"outcome":"applied"},{"worker":"b","ticket":2,"kind":"execute","key":["r","a"],"attempt":1,"outcome":"replayed"}],"effects":[{"key":["r","a"],"amount":5}]}}}'
# A stale transient never commits: the reclaim keeps attempt 1 and the retry budget is untouched.
check leased-stale-transient \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":[{"id":"s","needs":[],"amount":5,"failures":1}],'"$W"',"commands":[{"op":"start","run":"r"},{"op":"claim","worker":"a"},{"op":"call","worker":"a","ticket":1},{"op":"advance","by":5},{"op":"claim","worker":"b"},{"op":"deliver","ticket":1},{"op":"renew","worker":"a","ticket":1},{"op":"call","worker":"a","ticket":1},{"op":"call","worker":"b","ticket":2},{"op":"deliver","ticket":2}]}}' \
  '{"ok":true,"result":{"results":[null,{"ticket":1},{"kind":"execute","outcome":"transient"},null,{"ticket":2},{"committed":false},{"renewed":false},{"outcome":"stale"},{"kind":"execute","outcome":"applied"},{"committed":true}],"final":{"now":5,"workers":[{"id":"a","up":true},{"id":"b","up":true}],"runs":[{"id":"r","status":"succeeded","cancel_requested":false,"steps":[{"id":"s","status":"succeeded","attempts":1,"ready_at":0,"lease":null}]}],"calls":[{"worker":"a","ticket":1,"kind":"execute","key":["r","s"],"attempt":1,"outcome":"transient"},{"worker":"b","ticket":2,"kind":"execute","key":["r","s"],"attempt":1,"outcome":"applied"}],"effects":[{"key":["r","s"],"amount":5}]}}}'
# Repeated cancellation does not revoke a lookup lease; a saved lookup survives the worker crash.
check leased-repeated-cancel-keeps-lookup \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"start","run":"r"},{"op":"claim","worker":"a"},{"op":"cancel","run":"r"},{"op":"claim","worker":"b"},{"op":"cancel","run":"r"},{"op":"call","worker":"b","ticket":2},{"op":"crash","worker":"b"},{"op":"deliver","ticket":2},{"op":"restart","worker":"b"},{"op":"call","worker":"b","ticket":2},{"op":"deliver","ticket":2}]}}' \
  '{"ok":true,"result":{"results":[null,{"ticket":1},null,{"ticket":2},null,{"kind":"lookup","outcome":"missing"},null,{"committed":false},null,{"kind":"lookup","outcome":"missing"},{"committed":true}],"final":{"now":0,"workers":[{"id":"a","up":true},{"id":"b","up":true}],"runs":[{"id":"r","status":"cancelled","cancel_requested":true,"steps":[{"id":"a","status":"cancelled","attempts":1,"ready_at":0,"lease":null}]}],"calls":[{"worker":"b","ticket":2,"kind":"lookup","key":["r","a"],"attempt":1,"outcome":"missing"}],"effects":[]}}}'
# A failing run cannot be cancelled; its running branch drains by lookup to blocked.
check leased-failing-not-cancellable \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"max_attempts":1,"steps":[{"id":"x","needs":[],"amount":1,"failures":1},{"id":"y","needs":[],"amount":2}],'"$W"',"commands":[{"op":"start","run":"r"},{"op":"claim","worker":"a"},{"op":"claim","worker":"b"},{"op":"call","worker":"a","ticket":1},{"op":"deliver","ticket":1},{"op":"cancel","run":"r"},{"op":"claim","worker":"a"},{"op":"call","worker":"a","ticket":3},{"op":"deliver","ticket":3}]}}' \
  '{"ok":true,"result":{"results":[null,{"ticket":1},{"ticket":2},{"kind":"execute","outcome":"transient"},{"committed":true},null,{"ticket":3},{"kind":"lookup","outcome":"missing"},{"committed":true}],"final":{"now":0,"workers":[{"id":"a","up":true},{"id":"b","up":true}],"runs":[{"id":"r","status":"failed","cancel_requested":false,"steps":[{"id":"x","status":"failed","attempts":1,"ready_at":0,"lease":null},{"id":"y","status":"blocked","attempts":1,"ready_at":0,"lease":null}]}],"calls":[{"worker":"a","ticket":1,"kind":"execute","key":["r","x"],"attempt":1,"outcome":"transient"},{"worker":"a","ticket":3,"kind":"lookup","key":["r","y"],"attempt":1,"outcome":"missing"}],"effects":[]}}}'
# Runtime validation precedence and mode isolation.
check leased-unknown-worker-before-down \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"crash","worker":"a"},{"op":"renew","worker":"zz","ticket":9}]}}' \
  '{"ok":false,"error":{"code":"UNKNOWN_WORKER"}}'
check leased-down-before-ticket \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"crash","worker":"a"},{"op":"renew","worker":"a","ticket":9}]}}' \
  '{"ok":false,"error":{"code":"WORKER_DOWN"}}'
check leased-wrong-worker \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"start","run":"r"},{"op":"claim","worker":"a"},{"op":"call","worker":"b","ticket":1}]}}' \
  '{"ok":false,"error":{"code":"WRONG_WORKER"}}'
check leased-claim-overflow \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"advance","by":2147483647},{"op":"claim","worker":"a"},{"op":"start","run":"r"},{"op":"claim","worker":"a"}]}}' \
  '{"ok":false,"error":{"code":"TIME_OVERFLOW"}}'
check leased-rejects-tick \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],'"$W"',"commands":[{"op":"tick"}]}}' \
  '{"ok":false,"error":{"code":"INVALID_INPUT"}}'
check classic-rejects-lease-duration \
  '{"protocol_version":1,"task":"workflow-recovery","input":{"steps":['"$S"'],"lease_duration":3,"commands":[]}}' \
  '{"ok":false,"error":{"code":"INVALID_INPUT"}}'
exit $fail
