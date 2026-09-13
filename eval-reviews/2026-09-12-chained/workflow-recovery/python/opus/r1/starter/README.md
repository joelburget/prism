# Durable workflow runner (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains typed request/state records, upfront validation, graph checks, the idempotent mock service, the durable scheduler, and the snapshot serializer. `main.py` is the JSON process adapter. `test_workflow.py` holds extra traces beyond the public corpus; run `python3 -m unittest test_workflow`.

The baseline behavior is retained: DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension is implemented. `MockService` holds the effect set and per-key transient budget, so it survives `crash`/`restart` independently of the runner's durable run state; `execute` replays a recorded effect, serves the configured `failures` as transient responses, then records exactly one effect, and `lookup` reports effect existence without recording one. Keys are structured `(run_id, step_id)` tuples, never concatenated strings.

`Simulator.tick` begins an attempt durably before calling the service, so `crash_at="after_begin"` and `crash_at="after_call"` model the two loss windows; recovering a `running` step reissues its call with the same attempt number and does not consume budget. Committed transient responses either reschedule (`ready_at = now + retry_delay`, `TIME_OVERFLOW` past the bound) or fail the step and run, blocking remaining pending steps. Cancellation marks pending steps cancelled and reconciles a running step through `lookup` on a later tick, leaving existing effects intact. Availability is checked before run lookup; `tick`/`start`/`cancel`/`crash` while down are `PROCESS_DOWN` and `restart` while up is `PROCESS_UP`.

The checkpoint-two leased mode is selected by the `workers` field; without it the previous contract (including `tick` and its crash checkpoints) is served unchanged. `LeasedSimulator` keeps worker up/down flags, a durable `Lease` per running step, and a `TicketRecord` per acquisition that survives its lease. Tickets increase globally and never repeat; reclaiming running work keeps the same attempt number and logical key. `claim` recovers the earliest expired running lease before starting ready pending work, and captures `execute` for an active run or `lookup` for a cancelling/failing one. `call` is fenced on the current, live lease, returns `stale` otherwise, and replays its saved receipt without a second audit entry. `deliver` commits only an undelivered response of a current, live lease whose worker is up, so a stale success or transient can never change attempts, retry time, terminal status or a replacement's lease. Transient commits retry from delivery time; exhaustion blocks pending steps and, while other branches are still running, enters `failing` and expires their leases so they finish through lookup only. The first cancellation expires running leases the same way; a repeated one is a full no-op and never strands a fresh lookup lease.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 public baseline (the whole checkpoint-one suite) and 11 public extension cases pass (drop `--phase baseline` to run both).
