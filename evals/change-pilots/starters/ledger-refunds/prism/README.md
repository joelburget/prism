# Prism invoice ledger baseline

Requires Prism 0.22.0 and its native build toolchain. From this directory, run
`./build.sh` once, then `./run.sh`. Set `PRISM=/path/to/prism` to select a compiler.
The build writes the native executable and lineage data into ignored `.build/`.
The launcher only executes the existing binary; it never compiles per request.
Rebuild after source edits.

The executable reads one line containing the shared JSON request envelope and
writes one JSON response. Each invocation starts empty. All ledger behavior is
implemented natively in `main.pr`; there are no other language runtimes involved.

The `Mutation` and `Operation` algebraic types separate validated commands from
JSON. Immutable `Event` records retain accepted payloads, and `Ledger.events`
provides append-only history and the global successful-key index. Validation
finishes before `commit` applies allocations. `Projection` values maintain sorted
invoice balances; reports reconstruct event prefixes and audits expose immutable
payloads. Canonical JSON encoding compares object fields independently of their
order while preserving allocation array order. Monetary sums use arbitrary-precision `Int`.

The supported operations are invoice, payment, close, report, and audit. Refund and
reverse operations intentionally return `INVALID_OPERATION`; those are the change
exercise. Preserve command parsing, execution, and projection interfaces while
extending the domain.

From the public evaluation directory, after building:

```sh
python3 run.py run --task ledger-refunds --phase baseline \
  --command /absolute/path/to/this/directory/run.sh --timeout 30
```

Validated against all 24 public baseline cases. Public acceptance coverage is not
proof of the complete contract; no evaluator-only cases were consulted.
