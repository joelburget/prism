# Workflow runner (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains typed request/state records, upfront validation, graph checks, the mock service, scheduler, and snapshot serializer. `main.py` is the JSON process adapter.

Implements DAG validation, run creation, single-action ticks, time advances, immutable observations, deterministic run/definition ordering, durable retries with bounded attempts and backoff, crash/restart recovery of in-flight attempts (including checkpointed crashes before and after the external call), idempotent replay of successful effects, and cancellation with reconciliation of uncertain in-flight work via service lookup.

The mock service and its call/effect audit persist across simulated crashes; only the runner's in-progress attempt commit can be lost and later recovered. Idempotency keys are the structured `(run_id, step_id)` pair, not a concatenated string.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 19 public baseline cases and 38 public extension cases pass.
