# Workflow runner baseline (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` is the idempotent mock service (execute with replay/transient/applied outcomes, lookup, full audit); `Runner.pr` schedules and transitions immutable state; `Output.pr` serializes results; `main.pr` is the JSON process adapter.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension is implemented on top of the baseline: durable attempt begin, `after_begin`/`after_call` crash checkpoints, `crash`/`restart`, bounded retries with `retry_delay` deadlines (`TIME_OVERFLOW` on commit past the bound), budget exhaustion blocking the run, structured `[run, step]` idempotency keys with replay, in-flight recovery reusing the original attempt number, and cancellation reconciled by service lookup. `PROCESS_DOWN`, `PROCESS_UP`, `UNKNOWN_RUN` runtime errors follow the published contract. The mock's per-key failure counter is derived from its own audit of transient responses, so lookups never consume it.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 19 baseline and 38 extension public cases pass (drop `--phase baseline` to run both). Additional edge-case checks live in `extra_tests.py`; run `python3 extra_tests.py` after building.
