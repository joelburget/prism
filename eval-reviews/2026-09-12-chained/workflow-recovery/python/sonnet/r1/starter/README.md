# Workflow runner (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains two modes sharing the same step/graph validation:

* The checkpoint-one contract (no `workers` field): typed request/state records, upfront validation, graph checks, the mock service, a single-process scheduler, and snapshot serializer. Implements DAG validation, run creation, single-action ticks, time advances, immutable observations, deterministic run/definition ordering, durable retries with bounded attempts and backoff, crash/restart recovery of in-flight attempts (including checkpointed crashes before and after the external call), idempotent replay of successful effects, and cancellation with reconciliation of uncertain in-flight work via service lookup.
* The checkpoint-two contract (`workers` present): multiple workers hold expiring, fenced leases (`claim`/`renew`/`call`/`deliver`) instead of a single-process `tick`. Attempts, retries, cancellation and failure draining follow the same rules as checkpoint one, but effect execution/lookup and commit are split into separate steps so in-flight work can be reclaimed by another worker after a lease expires. Delivery is fenced so a stale ticket cannot overwrite a replacement's state, and cancellation/failure draining force-expire in-flight leases so uncertain work is reconciled through an audited service lookup rather than guessed from the effect list.

`main.py` inspects the request's `input` object for a `workers` field and dispatches to the matching simulator; both share `StepDefinition`, `MockService`, and graph validation from `workflow.py`.

The mock service and its call/effect audit persist across simulated crashes; only the runner's in-progress attempt commit (or, in leased mode, an undelivered ticket) can be lost and later recovered. Idempotency keys are the structured `(run_id, step_id)` pair, not a concatenated string.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 public baseline cases and 11 public extension cases pass.
