# Workflow runner (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the idempotent mock service, scheduler, recovery/cancellation logic, and snapshot serializer for the checkpoint-one contract. `leased.ts` implements checkpoint two (leased workers and fenced recovery) on top of the same validation helpers and mock service. `main.ts` is the JSON process adapter; it selects leased mode when the input carries a `workers` field and otherwise runs the unchanged checkpoint-one simulator.

Implemented behavior:

* Baseline: DAG validation, run creation, single-action ticks, time advances, immutable observations, deterministic run/definition ordering.
* Durable execution: each attempt begins once (`running`, `attempts += 1`) before the service call. Recovery after a crash reissues the same attempt number and structured `[run, step]` key.
* Mock service: per-key effect cache (`applied`/`replayed`), per-key transient failure counters, `lookup` that reports `found`/`missing`. Every call is audited; the service survives runner crashes.
* Retries: committed transient responses reschedule at `now + retry_delay` while `attempts < max_attempts`, otherwise the step fails, the run fails, and pending steps become `blocked`.
* Crash windows: `crash_at: after_begin` (before the call) and `after_call` (after the atomic service response, before commit). Idle checkpointed ticks stay up.
* Process state: `crash`, `restart` (passive), `PROCESS_DOWN` / `PROCESS_UP` errors, availability checked before run lookup.
* Cancellation: pending steps become `cancelled`; a running step leaves the run `cancelling` until a tick reconciles it by lookup (`found` -> succeeded, `missing` -> cancelled).

Checkpoint two (leased mode, `workers` present):

* Commands `start`, `advance`, `observe`, `crash`/`restart` per worker, `claim`, `renew`, `call`, `deliver`, `cancel`; each yields one value in `results`. `tick` and checkpoints are rejected here, and `lease_duration` is rejected in the old mode.
* Leases: `claim` first reclaims the earliest running step with an expired lease (`now >= expires`, including cancelling/failing runs, lookup kind), else starts the first ready pending step of an active run (execute kind). Tickets are global, monotonic, and never reused; reclaims keep the attempt number. A worker with a live lease is `WORKER_BUSY`.
* Fencing: `call` invokes the mock once per live ticket and saves the receipt; stale tickets get `{"outcome":"stale"}`. `deliver` commits only an undelivered response whose ticket is still the step's current live lease and whose owner is up. Retries are scheduled from delivery time.
* Cancellation expires all running leases once and recovers them via lookup (`found` -> succeeded, `missing` -> cancelled). Retry exhaustion with other running steps enters `failing`, expires their leases, and drains by lookup (`missing` -> blocked) before `failed`.
* Audit entries in leased mode carry `worker` and `ticket`; observations carry `now`, `workers`, `runs`, with a `lease` object or null on each step.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

`test.sh` runs additional edge-case traces against `run.sh` and checks the responses.
