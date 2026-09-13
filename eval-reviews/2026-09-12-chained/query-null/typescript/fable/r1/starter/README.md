# Query engine — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode). Unit tests run with `node --test` from this directory.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, join kinds, and ordering.

## NULL support

Values are JSON `null` for SQL NULL. Truth values are represented directly: TRUE is `true`, FALSE is `false`, UNKNOWN is `null`. WHERE, ON, and HAVING keep only `true`. Nullable columns, NULL literals, IS [NOT] NULL, COALESCE, LEFT [OUTER] JOIN, NULLS FIRST/LAST, and global SUM/MIN/MAX are all supported per the problem statement.

An untyped NULL literal has no type until its context demands one; the binder accepts it wherever an int, text, or bool is required. Every bound expression carries a conservative `nullable` flag derived from declared nullability, LEFT JOIN padding (the right table of a left join is nullable everywhere except inside its own ON clause), and aggregate emptiness (SUM/MIN/MAX are always nullable; COUNT never is).

## Optimizer

Constant folding is bottom-up and sound under three-valued logic: fully constant subexpressions are evaluated, NULL-strict operators with a NULL literal operand fold to NULL, FALSE absorbs AND and TRUE absorbs OR, TRUE/FALSE identities are dropped, double NOT collapses, and COALESCE drops NULL literals and everything after a non-nullable argument. The two-valued tautologies `x = x`, `p OR NOT p`, `p AND NOT p`, `x IS NULL`, and `x IS NOT NULL` fold only when the operand is provably non-nullable.

WHERE conjuncts that reference a single source are pushed into that source's scan when the source is never NULL-padded (the first table, or the right side of an inner join). For the padded side of a LEFT JOIN, a conjunct is pushed only when it is null-rejecting, tested by evaluating it against an all-NULL row; in that case the LEFT JOIN is reduced to an INNER JOIN first. Other conjuncts stay above the joins. ON conjuncts are never moved. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

Validation: all public baseline and extension cases pass in both optimizer modes. `starter.json` lists the export files. Build outputs and dependency installations are ignored.
