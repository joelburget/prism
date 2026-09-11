# TypeScript invoice ledger baseline

Requires Node.js 25.2.1 or newer. From this directory, run `./run.sh`; Node executes
the erasable TypeScript directly. No package install or transpilation is needed at
runtime. It reads one line containing the shared JSON request envelope and writes
one JSON response. Each invocation starts empty.

For strict static checking, run `npm ci --ignore-scripts` and `npm run typecheck`.
TypeScript 5.9.3 and Node typings 25.0.3 are pinned in the lockfile.

`ledger.ts` separates the discriminated command union and boundary validation,
append-only event history and the global successful-key index (`Ledger`), and
balance reconstruction (`Projection`). Allocation checks complete before commit.
Reports reconstruct fresh event-prefix projections; audit payloads are copied.
Canonical payload comparison sorts object keys and preserves array order. The
JSON reviver retains the distinction between integer tokens and floating tokens.
All bounded ledger sums fit exactly in JavaScript numbers; no bitwise arithmetic
is used.

The supported operations are invoice, payment, close, report, and audit. Refund and
reverse operations intentionally return `INVALID_OPERATION`; those are the change
exercise. Preserve the command parsing, ledger execution, and projection interfaces
while extending the domain.

From the public evaluation directory:

```sh
python3 run.py run --task ledger-refunds --phase baseline \
  --command /absolute/path/to/this/directory/run.sh --timeout 30
```

Validated against all 24 public baseline cases and strict type checking. Public
acceptance coverage is not proof of the complete contract; no evaluator-only cases
were consulted.
