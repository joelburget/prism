# Durable workflow runner (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` is the idempotent mock service and its audit; `Runner.pr` schedules and transitions immutable durable state for both the single-process and the leased runner; `Output.pr` serializes results; `main.pr` is the JSON process adapter and selects the mode.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension adds durable execution and recovery on top of that baseline. `Service.pr` implements one atomic interaction per call: an existing effect for the structured key `[run_id, step_id]` replays, the configured `failures` budget for that key returns transients, and the next call records exactly one effect. Lookup reports `found`/`missing` without recording effects or consuming the failure budget. Every call, including replays, transients and lookups, is audited.

`Runner.pr` begins an attempt durably before calling the service, so recovery reissues an in-flight attempt with its original attempt number and key instead of consuming a new attempt. A committed transient either schedules `ready_at = now + retry_delay` or, once the budget is exhausted, fails the step and run and blocks the remaining pending steps. `crash`/`restart` toggle process availability; `crash_at` injects a crash after the durable begin or after the atomic service response but before commit; ticks, starts, cancels and crashes while down return `PROCESS_DOWN` and restarting while up returns `PROCESS_UP`. Cancelling a nonterminal run cancels its pending steps, and an in-flight step is reconciled by a later tick through lookup: `found` succeeds that step, `missing` cancels it, and either outcome leaves the run cancelled.

An `input` without a `workers` field uses the single-process contract above, unchanged. An `input` with `workers` selects leased mode: several workers with separate availability, one durable lease `{worker, ticket, expires}` per running step, and the acquisition, service call and durable commit split into `claim`, `call` and `deliver`. A lease is live while `now < expires`; a ticket may call or commit only while it is still its step's current live lease and its worker is up, so a stale owner can neither reissue work nor overwrite a replacement's state. Tickets are a global, never-reused acquisition identity, distinct both from the logical key `[run_id, step_id]` and from the durable attempt number, which only a new pending attempt increments. `claim` recovers the earliest running step with an expired lease before starting the first ready pending step, taking call kind `execute` in an active run and `lookup` in a draining one. A delivered transient schedules `ready_at = delivery time + retry_delay` or, at budget exhaustion, fails the step, blocks pending steps and either fails the run or enters `failing`, expiring the remaining leases so the surviving branches reconcile through lookup alone. The first cancellation of a run likewise expires every running lease; repeated cancellation is a no-op that leaves fresh lookup leases intact. Results are reported per command in `results`, with `final` carrying the worker, lease, call and effect audit.

`selftest.py` runs extra traces against the built adapter (`./build.sh` first, then `python3 selftest.py`).

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 public baseline and 11 public extension cases pass; drop `--phase baseline` to run both phases.
