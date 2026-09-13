# Query engine — Typescript (checkpoint 2)

Node.js 25+ executes the erasable TypeScript directly. For static validation, run
`tsc --noEmit --typeRoots /opt/typescript/node_modules/@types -p tsconfig.json`
(TypeScript 5.9.3, strict mode).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and
writes one JSON response. Every request starts a fresh process. Domain errors
exit zero and contain only the fixed error code. No SQL library or external
query engine is used.

## Modules

- `model.ts`: wire validation for both the `queries` protocol and the new
  `commands`/view protocol, plus the shared AST/value types.
- `parser.ts` / `binder.ts`: tokenizer, precedence parser, and static
  type/name checking, including NULL literals, COALESCE, IS [NOT] NULL,
  LEFT [OUTER] JOIN, and NULLS FIRST/LAST.
- `engine.ts`: batch execution and optimization (constant folding plus
  single-source WHERE-to-scan pushdown that skips the right side of a LEFT
  JOIN) used directly by the one-shot `queries` protocol.
- `views.ts`: base-table storage (per-table row identity, encounter order,
  used-ID tracking) and incrementally maintained views for the `commands`
  protocol. Each view keeps, per join step, materialized intermediate tuples
  and a match index so that a single row insert/update/delete only touches
  the rows/groups it can affect (nested-loop against the *other*, already
  materialized side — not a full requery), including LEFT JOIN NULL-padding
  transitions, grouped aggregate revisits, and DISTINCT membership counts.
  ORDER BY/LIMIT/OFFSET are applied to the maintained result set at read time.

## Status

All public baseline (checkpoint 1) and extension (checkpoint 2) cases pass via
`python3 run.py run --task query-null --command './starter/run.sh'`.
