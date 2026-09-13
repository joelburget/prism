# Durable workflow runner (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` is the idempotent mock service and its audit; `Runner.pr` schedules and transitions immutable durable state; `Output.pr` serializes results; `main.pr` is the JSON process adapter.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension adds durable execution and recovery on top of that baseline. `Service.pr` implements one atomic interaction per call: an existing effect for the structured key `[run_id, step_id]` replays, the configured `failures` budget for that key returns transients, and the next call records exactly one effect. Lookup reports `found`/`missing` without recording effects or consuming the failure budget. Every call, including replays, transients and lookups, is audited.

`Runner.pr` begins an attempt durably before calling the service, so recovery reissues an in-flight attempt with its original attempt number and key instead of consuming a new attempt. A committed transient either schedules `ready_at = now + retry_delay` or, once the budget is exhausted, fails the step and run and blocks the remaining pending steps. `crash`/`restart` toggle process availability; `crash_at` injects a crash after the durable begin or after the atomic service response but before commit; ticks, starts, cancels and crashes while down return `PROCESS_DOWN` and restarting while up returns `PROCESS_UP`. Cancelling a nonterminal run cancels its pending steps, and an in-flight step is reconciled by a later tick through lookup: `found` succeeds that step, `missing` cancels it, and either outcome leaves the run cancelled.

`selftest.py` runs extra traces against the built adapter (`./build.sh` first, then `python3 selftest.py`).

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 19 public baseline and 38 public extension cases pass; drop `--phase baseline` to run both phases.
