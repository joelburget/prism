# Query engine — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions using the same three-valued evaluator as execution. It pushes single-source WHERE conjuncts into scans only for sources that are never NULL-padded (the initial source and INNER JOIN sources). Predicates on LEFT JOIN sources and conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, NULL literals, three-valued predicates, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, explicit NULLS FIRST/LAST ordering, and grouped/global COUNT/SUM/MIN/MAX. NULL is represented by JavaScript null, with a separate untyped NULL scalar type during binding. Bound expressions track nullability, including padding introduced by outer joins. Binding checks every argument before optimization or evaluation, including unreachable COALESCE arguments.

Validation: all 21 public baseline and 34 public extension cases pass. Run additional regressions with `python3 starter/test_extension.py` from the workspace root. Static validation is also available with `tsc --noEmit --typeRoots /opt/typescript/node_modules/@types` in this directory. Node executes the TypeScript sources directly; no build step is required. Build outputs and dependency installations are ignored.
