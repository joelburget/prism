# Workflow runner (typescript)

Requires Node.js 25 or later. Run `npm ci --ignore-scripts` and `npm run typecheck` for strict checking, then `./run.sh`. Node executes erasable TypeScript directly; npm dependencies are development tooling only.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`workflow.ts` contains typed request/state interfaces, upfront validation, graph checks, the mock service, scheduler, and snapshot serializer. `main.ts` is the JSON process adapter. `extra-tests.ts` holds agent-authored traces beyond the public corpus; run it with `node extra-tests.ts`.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension adds durable execution over that baseline. `MockService` keeps a two-level ledger keyed by run and then step, so structured idempotency keys never collapse into a concatenated string; it replays recorded effects, serves the configured transient failures per key, and audits every execute and lookup. `Simulator.tick` begins an attempt durably before the call, honours the `after_begin` and `after_call` crash checkpoints, and only then commits the response: success marks the step succeeded, a committed transient either schedules `now + retry_delay` or exhausts the budget and blocks the run's remaining pending steps. Recovering an in-flight attempt reissues the same attempt number and key, so lost responses cost service calls but not attempts. Cancellation cancels pending steps immediately and reconciles a running step through an audited lookup, preserving effects that already happened. `crash`, `restart`, and the process-availability checks precede any run lookup; observe and advance work in either state.

Checkpoint two adds leased workers. A request carrying `workers` selects the new
mode in `evaluateRequest`; without it the complete checkpoint-one contract runs
unchanged. `LeasedSimulator` splits the old tick into `claim`, `call` and
`deliver`: a claim persists a lease `{worker,ticket,expires}` with a globally
unique ticket, starting a new attempt for pending work and reusing the attempt
number when recovering a running step whose lease has expired. `call` is fenced
on the step's current, live lease, so a superseded owner gets `stale` and never
reaches the mock; the first live call saves its receipt and repeats replay it
without a new audit entry. `deliver` re-checks currency, liveness and the owner's
availability before committing, so a response produced before expiration cannot
overwrite a replacement's state. Cancellation and retry exhaustion expire the
remaining running leases, which routes recovery through audited lookups (the new
`failing` status drains a failed run) and makes an already applied effect
impossible to lose. `MockService` and `LeasedService` share one `EffectLedger`,
so the idempotency, transient-count and effect rules are literally the same code.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 public baseline and 11 public extension cases pass; drop `--phase baseline` to run both.
