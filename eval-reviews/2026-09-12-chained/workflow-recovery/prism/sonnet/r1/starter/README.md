# Workflow runner with durable recovery (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` implements the idempotent mock service (execute/lookup, replay, transient failures); `Runner.pr` schedules and transitions immutable state, including retries, crash checkpoints, and cancellation reconciliation; `Output.pr` serializes results; `main.pr` is the JSON process adapter.

The runner supports DAG validation, run creation, single-action ticks, time advances, immutable observations, multiple independent runs, bounded retries with durable backoff deadlines, `crash`/`restart` with `after_begin`/`after_call` checkpoints, and `cancel` with lookup-based reconciliation of an uncertain in-flight attempt. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 public cases (19 baseline, 38 extension) pass.
