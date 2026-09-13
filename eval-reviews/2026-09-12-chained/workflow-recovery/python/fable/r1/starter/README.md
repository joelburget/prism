# Workflow runner baseline (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains typed request/state records, upfront validation, graph checks, the mock service, scheduler, and snapshot serializer. `main.py` is the JSON process adapter.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension is implemented: durable begin/commit boundaries with `after_begin`/`after_call` crash checkpoints, `crash`/`restart`, bounded retries with `retry_delay` deadlines, run failure with blocked pending steps, `cancel` with lookup-based reconciliation of in-flight attempts, and an idempotent mock service whose audit (`execute` applied/replayed/transient, `lookup` found/missing) and effects survive runner crashes. `test_workflow.py` holds additional unit checks.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All public baseline and extension cases pass (drop `--phase baseline` to run both).

## Checkpoint two: leased workers

Requests with a `workers` field use `leased.py`, which implements the leased-worker
mode: `claim`/`renew`/`call`/`deliver` split the old tick into acquisition, an
audited mock call with a saved transport message, and a fenced commit. Commits
require the ticket to be the step's current live lease with its owner up, so a stale
owner cannot overwrite a replacement. Cancellation and retry exhaustion expire live
leases immediately; the resulting `cancelling`/`failing` runs recover through audited
lookups only. `main.py` dispatches on the presence of `workers`; requests without it
take the unchanged checkpoint-one path in `workflow.py`. `test_leased.py` holds unit
checks for the new interleavings.
