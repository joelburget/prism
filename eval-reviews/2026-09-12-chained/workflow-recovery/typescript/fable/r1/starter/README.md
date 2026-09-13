# Workflow runner (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the idempotent mock service, scheduler, recovery/cancellation logic, and snapshot serializer. `main.ts` is the JSON process adapter.

Implemented behavior:

* Baseline: DAG validation, run creation, single-action ticks, time advances, immutable observations, deterministic run/definition ordering.
* Durable execution: each attempt begins once (`running`, `attempts += 1`) before the service call. Recovery after a crash reissues the same attempt number and structured `[run, step]` key.
* Mock service: per-key effect cache (`applied`/`replayed`), per-key transient failure counters, `lookup` that reports `found`/`missing`. Every call is audited; the service survives runner crashes.
* Retries: committed transient responses reschedule at `now + retry_delay` while `attempts < max_attempts`, otherwise the step fails, the run fails, and pending steps become `blocked`.
* Crash windows: `crash_at: after_begin` (before the call) and `after_call` (after the atomic service response, before commit). Idle checkpointed ticks stay up.
* Process state: `crash`, `restart` (passive), `PROCESS_DOWN` / `PROCESS_UP` errors, availability checked before run lookup.
* Cancellation: pending steps become `cancelled`; a running step leaves the run `cancelling` until a tick reconciles it by lookup (`found` -> succeeded, `missing` -> cancelled).

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

`test.sh` runs additional edge-case traces against `run.sh` and checks the responses.
