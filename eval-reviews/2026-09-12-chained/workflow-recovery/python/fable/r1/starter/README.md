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
