# Query engine with NULL support — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode). `npm test` runs `tests.ts`, a white-box suite over the optimizer.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, join kinds, and ordering.

SQL NULL is JavaScript `null` in every value position, so UNKNOWN is simply a `null` predicate result. Predicates are applied through a single `holds` helper that keeps only TRUE, which is what makes FALSE and UNKNOWN behave identically in ON, WHERE, and HAVING. AND and OR absorb UNKNOWN before their operands are inspected; every other scalar operator propagates it. Aggregates skip NULL inputs and return NULL when nothing is left, while COUNT stays non-null. An outer join pads unmatched left rows with NULL for the whole right side, regardless of declared nullability.

The binder computes a conservative `nullable` flag for each expression, marking the padded side of every outer join nullable even when its columns are declared `NOT NULL`. An untyped NULL literal carries no type and unifies with whatever its context requires, without weakening type checking elsewhere.

Optimization performs bottom-up folding of constant scalar expressions, including NULL-valued ones, and pushes single-source WHERE conjuncts into join input scans. Conjuncts referencing multiple sources remain above the join. Three rules depend on the nullability flags: `x = x` and `p OR NOT p` fold only over non-nullable operands; a conjunct is never pushed below an outer join into its padded input; and a WHERE conjunct that rejects an all-NULL row provably discards every padded row, so it converts that outer join into an inner join and then becomes eligible for pushdown. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting with explicit NULL placement, and pagination preserve the documented ordering in either mode.

Supported: SELECT/WHERE, inner and LEFT [OUTER] joins, nullable schemas, NULL/IS [NOT] NULL/COALESCE, grouped and global COUNT/SUM/MIN/MAX, GROUP BY/HAVING, DISTINCT/ORDER BY with NULLS FIRST|LAST/LIMIT/OFFSET.

Validation: all 21 public baseline and 34 public extension cases pass, in both optimizer modes. This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
