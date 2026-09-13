# Workflow runner baseline (python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.py` contains typed request/state records, upfront validation, graph checks, the durable simulator, idempotent mock service, scheduler, recovery logic, and snapshot serializer. `main.py` is the JSON process adapter.

The runner supports DAG validation, run creation, deterministic single-action scheduling, durable retries, simulated crashes and recovery, cancellation reconciliation, and idempotent external effects. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

The implementation covers the original durable-recovery protocol and checkpoint
two's opt-in `workers` mode. The latter models worker leases, globally fenced
tickets, split claim/call/deliver transport, renewal and worker availability,
plus cancellation and failure reconciliation by audited lookups.
