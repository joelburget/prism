# Query engine — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions using the same three-valued evaluator as execution. It pushes single-source WHERE conjuncts into scans only for sources that are never NULL-padded (the initial source and INNER JOIN sources). Predicates on LEFT JOIN sources and conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, NULL literals, three-valued predicates, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, explicit NULLS FIRST/LAST ordering, and grouped/global COUNT/SUM/MIN/MAX. NULL is represented by JavaScript null, with a separate untyped NULL scalar type during binding. Bound expressions track nullability, including padding introduced by outer joins. Binding checks every argument before optimization or evaluation, including unreachable COALESCE arguments.

Validation: all 21 public baseline and 34 public extension cases pass. Run additional regressions with `python3 starter/test_extension.py` from the workspace root. Static validation is also available with `tsc --noEmit --typeRoots /opt/typescript/node_modules/@types` in this directory. Node executes the TypeScript sources directly; no build step is required. Build outputs and dependency installations are ignored.

Checkpoint two adds `database`/`commands` requests through `views.ts`. The old
`queries` path still uses the existing executor. Both paths share parsing,
binding, optimization, scalar evaluation, DISTINCT, stable sorting and windows.
The launch entrypoint remains unchanged.

Batches validate all change shapes first, then use private per-ID overlays to
validate ordered row operations. Only after validation succeeds do final deltas
reach views and base state. Successful insertions reserve IDs even if deleted
in the same batch. Encounter positions are separate from row IDs. Each view
checks table dependencies before maintenance, so unrelated changes leave its
cached state untouched.

Join stages retain left prefixes, right rows, equality-key indexes and results
per left prefix. Old/new right keys identify affected prefixes; changed left
prefixes are always included. Each stage updates both inputs before replacing
affected contributions, including NULL placeholders. Chained stages propagate
only actual changes. Non-equality predicates use a general candidate scan and
always evaluate the original ON expression. Large matching buckets or general
joins can therefore require substantial work proportional to their candidates.

Groups retain bag membership, COUNT/SUM accumulators, MIN/MAX value multiplicities,
and an indexed heap for first encounter order. Retraction of the last current
extremum may scan that group's distinct values. SELECT/HAVING refresh only touched
groups. Reads assemble retained projections in encounter order and share the old
DISTINCT/sort/window code; the resulting snapshot is cached until a relevant
change. Previously returned arrays and row values are never mutated.

Checkpoint-two validation (run from `/work`):

- `python3 run.py run --task query-null --command './starter/run.sh'`:
  baseline **55/55**, extension **20/20**.
- `node starter/test_views.ts`: **15,744** differential snapshots against fresh
  checkpoint-one queries, in both optimizer modes, plus lifecycle, rollback and
  numeric wire-error regressions.
- `python3 starter/test_extension.py`: the preceding submission's 12 additional
  scenarios pass in both optimizer modes.
- `cd starter && tsc --noEmit --typeRoots /opt/typescript/node_modules/@types`:
  passes. Node runs erasable TypeScript directly; no build step is needed.

Scaling, separate from acceptance: `node starter/benchmark_views.ts` measures six
views with 800 apply/read pairs. Local medians over five runs were **23.6 ms** for
2,000 base rows and **73.9 ms** for 20,000 base rows, including initial creation.
This is a local probe, with no acceptance threshold; supplied scaling workloads
were not present in this workspace.

Review effort, separate from test/scaling results: one dedicated manual source
review pass checked final-batch join invalidation, NULL-key exclusions, bag
identity, heap retractions, empty global groups, optimizer reuse, validation
precedence, ID reservation, and snapshot ownership. This was an implementation
self-review, not an independent review.
