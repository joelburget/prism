# Workflow runner (Python)

Requires Python 3.10 or later. Run `./run.sh`; no build or dependencies are needed.
The launcher works from any working directory and reads one JSON request from
stdin, returning one JSON response.

`workflow.py` contains typed request/state records, upfront validation, graph
checks, the mock service, scheduler, and immutable snapshot serializer. `main.py`
is the JSON adapter.

The runner supports dependency scheduling, independent runs, bounded retries,
simulated crash/restart recovery, cancellation, and idempotent external effects.
Run and step state persist across simulated crashes. Service effects, per-key
failure counters, and ordered call audits persist independently. Checkpoints
interrupt attempts before the call or before committing its response; recovery
reuses the durable attempt number. Cancellation reconciles uncertain work using
service lookup without executing new effects.

Run additional regression checks with:

```sh
python3 -m unittest discover -s starter -p 'test_*.py'
```

From the public corpus root, run the full acceptance suite with:

```sh
python3 run.py run --task workflow-recovery --command './starter/run.sh'
```
