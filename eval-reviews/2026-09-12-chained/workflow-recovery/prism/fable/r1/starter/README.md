# Workflow runner baseline (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits. `build.sh` routes the backend C compiler through `cc.sh`, which forwards to `PRISM_CC` (default `/usr/lib/llvm-22/bin/clang`) minus `-flto=thin`: the program now has more function-group objects than GNU ld's ThinLTO plugin can hold open under the container's 256-descriptor limit, and a native link of the same objects is behaviorally identical.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` is the idempotent mock service (execute with replay/transient/applied outcomes, lookup, full audit); `Runner.pr` schedules and transitions immutable state for the checkpoint-one contract; `Leased.pr` is the checkpoint-two leased-worker runner; `Output.pr` serializes both result shapes; `main.pr` is the JSON process adapter.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

The extension is implemented on top of the baseline: durable attempt begin, `after_begin`/`after_call` crash checkpoints, `crash`/`restart`, bounded retries with `retry_delay` deadlines (`TIME_OVERFLOW` on commit past the bound), budget exhaustion blocking the run, structured `[run, step]` idempotency keys with replay, in-flight recovery reusing the original attempt number, and cancellation reconciled by service lookup. `PROCESS_DOWN`, `PROCESS_UP`, `UNKNOWN_RUN` runtime errors follow the published contract. The mock's per-key failure counter is derived from its own audit of transient responses, so lookups never consume it.

## Checkpoint two: leased workers

A request with a `workers` field selects the leased protocol (`Leased.pr`); without it the frozen checkpoint-one path runs unchanged, including its command vocabulary and response shape. The leased simulator keeps `now`, per-worker up/down flags, runs whose running steps carry one durable lease `{worker, ticket, expires}`, a global ticket counter, and ticket metadata (owner, key, attempt number, call kind, saved response, delivered flag) that survives worker crashes like a delayed transport message. `claim` picks the earliest expired running step (any nonterminal run, lookup kind for cancelling/failing runs) before the first ready pending step of an active run, and reclaiming keeps the attempt number while consuming a fresh ticket. `call` is fenced: it acts only for the step's current live lease, invokes the mock exactly once per ticket, and replays the saved receipt otherwise. `deliver` commits only an undelivered response whose ticket is still the current live lease and whose owner is up, then clears the lease; transient responses schedule retries from delivery time, exhaustion drains other in-flight branches through the `failing` state, and the first cancellation expires every running lease so recovery proceeds by audited lookup. Each command yields one immutable result value; audit calls carry `worker` and `ticket`. Runtime errors (`UNKNOWN_WORKER`, `WORKER_DOWN`, `WORKER_UP`, `WORKER_BUSY`, `UNKNOWN_TICKET`, `WRONG_WORKER`, `UNKNOWN_RUN`, `DUPLICATE_RUN`, `TIME_OVERFLOW`) follow the published precedence, and more than 2,000 commands is `INVALID_INPUT`.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

All 57 baseline (the entire checkpoint-one public suite) and 11 checkpoint-two extension public cases pass (drop `--phase baseline` to run both). Additional edge-case checks for both checkpoints live in `extra_tests.py`; run `python3 extra_tests.py` after building.
