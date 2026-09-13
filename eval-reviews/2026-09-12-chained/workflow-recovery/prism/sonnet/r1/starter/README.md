# Workflow runner with durable recovery (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits. `build.sh` routes the C compiler through `cc-wrapper.sh`, which serializes LTO backend codegen (`-flto-jobs=1`); this only affects build-time parallelism, not program behavior.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records for both the checkpoint-one single-process runner and the checkpoint-two leased-worker runner; `Validation.pr` validates inputs and the graph for both modes; `Service.pr` implements the idempotent mock service (execute/lookup, replay, transient failures) shared by both; `Runner.pr` schedules and transitions immutable state for both the original tick-based simulator and the leased-worker simulator (claim/renew/call/deliver, fencing, failure draining); `Output.pr` serializes results for both response shapes; `main.pr` is the JSON process adapter that dispatches on the presence of a top-level `workers` field.

Without `workers`, the request uses the complete checkpoint-one contract: DAG validation, run creation, single-action ticks, time advances, immutable observations, multiple independent runs, bounded retries with durable backoff deadlines, `crash`/`restart` with `after_begin`/`after_call` checkpoints, and `cancel` with lookup-based reconciliation of an uncertain in-flight attempt.

With `workers`, the request uses the checkpoint-two leased mode: multiple workers each claim at most one live lease at a time, `call` and `deliver` are split so a service response can be produced by one worker and committed by another (or lost to a crash), tickets are fencing tokens distinct from logical attempts, and cancellation/failure draining expire in-flight leases so a stale owner cannot resurrect superseded progress.

All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 68 public cases (57 baseline, 11 extension) pass.
