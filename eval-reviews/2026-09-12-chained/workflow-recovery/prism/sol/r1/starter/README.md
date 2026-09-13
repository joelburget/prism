# Workflow runner baseline (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates the envelope, inputs, and graph; `Service.pr` implements the idempotent mock and audit; `Runner.pr` schedules and transitions immutable durable state; `Output.pr` serializes results; `main.pr` is the JSON process adapter.

The runner supports DAG validation, deterministic scheduling, durable crash/restart recovery, bounded delayed retries, cancellation reconciliation, immutable observations, and exactly-once effect recording through structured idempotency keys. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

The implementation preserves the baseline launch and build interfaces while implementing the published recovery extension.
