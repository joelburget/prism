# Workflow runner baseline (prism)

Requires Prism 0.18.0 on PATH (or set `PRISM` to its executable path). Run `./build.sh` once, then `./run.sh`. The launcher executes `.build/workflow` without recompiling. Rebuild after source edits.

Read one newline-terminated workflow-recovery request from stdin and write one response to stdout. Every request creates a fresh simulator and service. The launcher works from any working directory.

`Domain.pr` defines typed request/state records; `Validation.pr` validates inputs and the graph; `Service.pr` records successful calls and effects; `Runner.pr` schedules and transitions immutable state; `Output.pr` serializes results; `main.pr` is the JSON process adapter.

The baseline supports DAG validation, run creation, single-action ticks, time advances, immutable observations, successful external calls, and deterministic run/definition ordering. All field/scalar checks finish before graph validation and command execution. Errors stop execution and return only the error envelope.

Recovery, persistence, retries, cancellation, service lookup, and idempotent replays are intentionally unimplemented. Valid extension commands/checkpoints and positive service failure configurations return `UNSUPPORTED_FEATURE`; invalid extension field shapes still return `INVALID_INPUT`. Configuration defaults/ranges are validated and retained for the extension.

From the public corpus root, validate with:

```sh
python3 run.py run --task workflow-recovery --phase baseline --command '/absolute/path/to/this/starter/run.sh' --timeout 30
```

The 19 public baseline cases pass. The public specification defines the extension to implement. This is baseline authoring material, not a completed extension submission.
