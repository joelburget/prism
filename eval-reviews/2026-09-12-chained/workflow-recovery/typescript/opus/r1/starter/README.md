# Workflow runner (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the mock service, scheduler, and snapshot serializer. `main.ts` is the JSON process adapter. `extra-tests.ts` holds agent-authored traces beyond the public corpus; run it with `node extra-tests.ts`.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension adds durable execution over that baseline. `MockService` keeps a two-level ledger keyed by run and then step, so structured idempotency keys never collapse into a concatenated string; it replays recorded effects, serves the configured transient failures per key, and audits every execute and lookup. `Simulator.tick` begins an attempt durably before the call, honours the `after_begin` and `after_call` crash checkpoints, and only then commits the response: success marks the step succeeded, a committed transient either schedules `now + retry_delay` or exhausts the budget and blocks the run's remaining pending steps. Recovering an in-flight attempt reissues the same attempt number and key, so lost responses cost service calls but not attempts. Cancellation cancels pending steps immediately and reconciles a running step through an audited lookup, preserving effects that already happened. `crash`, `restart`, and the process-availability checks precede any run lookup; observe and advance work in either state.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 19 public baseline and 38 public extension cases pass; drop `--phase baseline` to run both.
