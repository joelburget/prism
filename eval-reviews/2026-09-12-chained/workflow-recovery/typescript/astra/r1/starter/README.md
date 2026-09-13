# Durable workflow runner (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the mock service, scheduler, and snapshot serializer. `main.ts` is the JSON process adapter.

The runner supports DAG validation, run creation, single-action ticks, time advances, immutable observations, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

Durable running attempts survive simulated crashes and are reissued with the same attempt number. The independent mock service records idempotent effects by structured run/step key and audits executions and cancellation lookups. Committed transient responses schedule bounded retries; exhaustion blocks remaining pending work. Cancellation reconciles uncertain actions by lookup without initiating an effect. Restart only restores process availability; subsequent ticks perform recovery.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

The public suite covers both baseline and recovery behavior. Persistence is modeled within one request; no filesystem storage, network calls, or operating-system crashes are involved.
