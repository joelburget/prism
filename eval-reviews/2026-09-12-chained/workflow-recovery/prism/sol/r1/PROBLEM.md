# Durable workflow recovery

## Change request

Extend an existing, deterministic in-memory workflow runner with durable execution,
restart recovery, bounded retries, cancellation, and idempotent external actions.
The baseline already schedules dependency graphs and invokes a mock external
service. Keep those behaviors working while introducing persistence boundaries.
The benchmark adapter models crashes explicitly; it never kills an operating
system process and uses no network, wall clock, or filesystem persistence.

Starter code is supplied with each evaluation run. Each run receives
only its assigned language's starter. Prism and the comparison language run in
separate, fresh agent sessions with no access to each other's code or run artifacts,
as described in the parent README. The starters must implement the
baseline contract and pass all `baseline` cases. The requested extension must
pass both phases. This document and `cases.json` are public acceptance material,
not a hidden evaluation or a reference implementation.

## Adapter contract

Read one JSON request from standard input, write exactly one JSON response, and
exit successfully, including for domain errors. The envelope is:

```json
{"protocol_version":1,"task":"workflow-recovery","input":{"steps":[{"id":"charge","needs":[],"amount":25}],"commands":[{"op":"start","run":"order-1"},{"op":"tick"}]}}
```

Return `{"ok":true,"result":RESULT}` or
`{"ok":false,"error":{"code":"CODE"}}`. No extra response fields. Diagnostics
may go to stderr. Each request starts a fresh simulator and mock service; all
commands within it share state. Object key order is irrelevant; array order is
significant. All numbers are integers.

`input` has required `steps` and `commands`, optional `max_attempts` (default 3)
and `retry_delay` (default 2). A step has required `id`, `needs`, and `amount`,
and optional `failures` (default 0). Step IDs and run IDs match
`[A-Za-z0-9_-]{1,64}`. Amount is an integer in 1..1,000,000; `failures` is an
integer in 0..100; `max_attempts` is in 1..10; `retry_delay` is in 1..1,000,000.
`steps` is nonempty. Dependencies name distinct other steps; forward references
are permitted; cycles are rejected. Multiple runs share this graph but have
independent state, service keys, and failure counters. `commands` may be empty.

Reject missing/unknown fields, wrong types (including booleans used as numbers),
invalid ranges/IDs, unknown commands, and invalid checkpoint names with
`INVALID_INPUT`. Validate all field shapes and scalar constraints before
executing any command. Then validate graph uniqueness (`DUPLICATE_STEP`),
dependency existence (`UNKNOWN_DEPENDENCY`), and acyclicity (`DEPENDENCY_CYCLE`),
in that order. Repeated dependencies are `INVALID_INPUT`; self-dependencies
are `DEPENDENCY_CYCLE`. Tests isolate errors instead of depending on ordering
among errors in the same category.

## Baseline behavior

Initially time is 0, the process is up, and there are no runs, calls, or effects.
The baseline supports `start`, `tick` without a checkpoint, `advance`, and
`observe`, successful actions, dependency scheduling, multiple runs, and the
shared input/graph validation. Baseline cases never configure service failures
or require crashes, retries, or cancellation.

* `{"op":"start","run":"r"}` creates an active run. Each step is pending,
  has zero attempts, and has `ready_at` equal to the current time. Reusing a run
  ID, including a terminal run, is `DUPLICATE_RUN`.
* `{"op":"tick"}` executes at most one action. First choose the oldest run
  containing an in-flight (`running`) step. Otherwise scan runs in creation
  order and steps in definition order, choosing the first pending step whose
  run is active, whose dependencies succeeded, and whose `ready_at <= now`.
  Waiting steps do not prevent later ready steps/runs from executing.
  A tick with no eligible work has no effect. It does not advance time.
* `{"op":"advance","by":N}` advances simulated time, including while the
  process is down. N must be an integer in 0..2,147,483,647; an out-of-range N
  is `INVALID_INPUT` during upfront shape/scalar validation. For a valid N,
  if `now + N` exceeds 2,147,483,647, execution fails with `TIME_OVERFLOW`.
* `{"op":"observe"}` appends a snapshot of durable runner state, including
  process availability and time. It works even while down and changes nothing.

Starting a new attempt durably changes its step from pending to running and
increments `attempts`. The service is then called. On successful completion the
step becomes succeeded. When all steps succeed, the run becomes succeeded.
`ready_at` is retained after success, failure, cancellation, and blocking; it
changes only when scheduling a retry. Dependent steps retain their original
`ready_at`; readiness also depends on dependency success.

## Extension: effects, retries, and persistence

The durable store contains run creation order, run and step states, attempt
numbers, cancellation flags, and retry deadlines. Time belongs to the simulator
and survives crashes. The mock service and its audit survive crashes separately
from the runner's durable store. Graph/configuration are immutable request data.
A restart must not reconstruct progress by guessing from the final effect list:
reconciliation must use the service interface and produce its audit calls.

Each logical action has the structured idempotency key `[run_id, step_id]`.
Use both components without ambiguous string concatenation. It is stable across
attempts and restarts and distinct across runs. The runner supplies the step's
amount and current durable attempt number to the service.

An **execute** call behaves atomically:

1. If this key already has an effect, return `replayed`, adding no effect.
2. Otherwise the first `failures` execute calls for this key return `transient`
   with no effect. Transient responses are not cached as successful results.
3. The next call records one effect and returns `applied`.

Every call is audited, including replays and transient failures. Successful
`applied` and `replayed` responses have the same runner semantics. On a committed
transient response, if `attempts < max_attempts`, set the step to pending and
`ready_at = now + retry_delay`. If that sum exceeds the time bound, return
`TIME_OVERFLOW`. Otherwise, when the attempt budget is exhausted, mark the step
failed, the run failed, and all its remaining pending steps blocked, including
independent branches. Already succeeded steps stay succeeded. Other runs can
continue. No terminal run ever executes new work.

An attempt begins once, before the external call. Recovering an in-flight
attempt reissues it using the **same attempt number and idempotency key**. It
does not consume a new attempt. Thus an uncommitted transient response may be
retried during recovery without increasing `attempts`; the budget limits
committed retry decisions, not the number of network calls. This distinction
must be observable in the service audit.

* `{"op":"crash"}` sets the process down, discarding volatile work only.
* `{"op":"restart"}` sets it up. It performs no service calls or status
  transitions. In-flight work is handled by a subsequent tick.
* `{"op":"tick","crash_at":"after_begin"}` crashes after recording the
  running attempt but before the service call. For an already running attempt,
  this means before reissuing its call, without incrementing the attempt.
* `{"op":"tick","crash_at":"after_call"}` crashes after the atomic service
  response/effect but before committing that response to the durable runner.
  This checkpoint applies to transient responses as well as success.

Checkpointed ticks with no eligible work stay up: no checkpoint is reached.
Ticks, starts, cancels, and crashes while down return `PROCESS_DOWN`.
Restarting while up returns `PROCESS_UP`. Observe/advance work in either state.
Errors stop command execution and produce only the error envelope, with no
partial result. Runtime checks happen in command order; process availability is
checked before looking up a run.

## Extension: cancellation races

`{"op":"cancel","run":"r"}` returns `UNKNOWN_RUN` for an unknown run.
Cancellation of a terminal (succeeded, failed, cancelled) run is a no-op,
including leaving its cancellation flag unchanged. For a nonterminal run, set
`cancel_requested` true and mark all pending steps cancelled. If there is no
running step, the run becomes cancelled immediately. Otherwise it becomes
cancelling; the running step remains uncertain until a tick reconciles it.
Repeated cancellation is harmless.

For a running step in a cancelling run, tick performs an atomic service
**lookup**, using the same key and durable attempt number. It never executes a
new effect. `found` means an effect exists: mark that step succeeded. `missing`
means no effect exists: mark it cancelled. Either result makes the run cancelled,
even if the running step was its final step. This prevents a cancellation after
an effect-but-before-commit crash from erasing an effect that already happened,
and prevents a pre-effect crash followed by cancellation from initiating work.
Existing effects are not compensated or refunded. This is exactly-once *effect
recording by the specified idempotent mock*, not a promise about arbitrary
external services.

Checkpointed ticks also apply during reconciliation: `after_begin` is before
lookup and leaves the existing attempt unchanged; `after_call` is after lookup
but before storing its result. Lookup is audited, does not affect the configured
failure counter, and can safely be repeated after another crash.

## Observable result

RESULT is exactly:

```json
{"observations":[],"final":{"now":0,"up":true,"runs":[],"calls":[],"effects":[]}}
```

Each observation contains only `now`, `up`, and `runs`; it is an immutable copy
at the time of observe. Final additionally contains the complete service audit
and effect list. Runs are in creation order. Each run is
`{"id":"r","status":"active","cancel_requested":false,"steps":[...]}`.
Run statuses are `active`, `succeeded`, `failed`, `cancelling`, `cancelled`.
Steps are in definition order, each exactly
`{"id":"s","status":"pending","attempts":0,"ready_at":0}`.
Step statuses are `pending`, `running`, `succeeded`, `failed`, `blocked`,
`cancelled`.

Calls are in invocation order, each exactly
`{"kind":"execute","key":["r","s"],"attempt":1,"outcome":"applied"}`
or the same shape with `kind: "lookup"` and outcome `found`/`missing`.
Execute outcomes are `applied`, `replayed`, `transient`. Effects are in first
application order, each exactly `{"key":["r","s"],"amount":25}`.
No call or effect may be synthesized, omitted, reordered, or deduplicated in
these audit arrays. Observations and audits are part of correctness, not debug
output. The adapter may translate internal representations to this schema.

## Acceptance intent and boundaries

The fixed suite checks existing graph behavior, independence of runs, retry
budgets/deadlines, both crash windows, repeated recovery, successful and failed
responses lost before commit, cancellation before/after effects, and input
errors. Cross-feature traces test that progress survives crashes while cancelled
or failed work does not run. These tests are language-neutral: use the same
request/expected-response files with either implementation command through the
shared runner documented in the parent directory.

This is deliberately a sequential deterministic model. Concurrent workers,
leases, database isolation, distributed clocks, arbitrary external side effects,
compensation, workflow-definition migration, and actual OS durability are out of
scope. A production implementation would need additional guarantees. A passing
public suite is evidence on its covered traces, not proof over all traces.
