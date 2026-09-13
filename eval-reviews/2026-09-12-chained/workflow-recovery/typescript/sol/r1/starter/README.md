# Workflow runner baseline (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the durable runner model, idempotent mock service, scheduler, and snapshot serializer. `main.ts` is the strict JSON process adapter.

The runner supports DAG validation, run creation, deterministic single-action ticks, time advances, immutable observations, durable crash recovery, bounded retries, cancellation reconciliation, and idempotent external effects. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

The command runs both baseline and extension acceptance phases.
