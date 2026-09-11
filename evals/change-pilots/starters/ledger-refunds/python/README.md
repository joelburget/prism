# Python invoice ledger baseline

Requires Python 3.10 or newer; validated with Python 3.14.7. No external packages.
From this directory, run `./run.sh`. It reads one line containing the shared JSON
request envelope and writes one JSON response. Each invocation starts empty.

`ledger.py` separates validated, frozen command records (`parse_operation`),
append-only `Event` history and the global successful-key index (`Ledger`), and
balance reconstruction (`Projection`). Validation completes before committing any
allocation. Reports rebuild an event prefix into a fresh projection; audits copy
payloads, preserving earlier results. Python integers retain exact monetary sums.

The supported operations are invoice, payment, close, report, and audit. Refund and
reverse operations intentionally return `INVALID_OPERATION`; those are the change
exercise. Preserve the command parsing, ledger execution, and projection interfaces
while extending the domain.

From the public evaluation directory:

```sh
python3 run.py run --task ledger-refunds --phase baseline \
  --command /absolute/path/to/this/directory/run.sh --timeout 30
```

Validated against all 24 public baseline cases. Public acceptance coverage is not
proof of the complete contract; no evaluator-only cases were consulted.
