# Durable workflow runner (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains typed request/state records, upfront validation, graph checks, the idempotent mock service, the durable scheduler, and the snapshot serializer. `main.py` is the JSON process adapter. `test_workflow.py` holds extra traces beyond the public corpus; run `python3 -m unittest test_workflow`.

The baseline behavior is retained: DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension is implemented. `MockService` holds the effect set and per-key transient budget, so it survives `crash`/`restart` independently of the runner's durable run state; `execute` replays a recorded effect, serves the configured `failures` as transient responses, then records exactly one effect, and `lookup` reports effect existence without recording one. Keys are structured `(run_id, step_id)` tuples, never concatenated strings.

`Simulator.tick` begins an attempt durably before calling the service, so `crash_at="after_begin"` and `crash_at="after_call"` model the two loss windows; recovering a `running` step reissues its call with the same attempt number and does not consume budget. Committed transient responses either reschedule (`ready_at = now + retry_delay`, `TIME_OVERFLOW` past the bound) or fail the step and run, blocking remaining pending steps. Cancellation marks pending steps cancelled and reconciles a running step through `lookup` on a later tick, leaving existing effects intact. Availability is checked before run lookup; `tick`/`start`/`cancel`/`crash` while down are `PROCESS_DOWN` and `restart` while up is `PROCESS_UP`.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 19 public baseline and 38 public extension cases pass (drop `--phase baseline` to run both).
