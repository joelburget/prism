# Query engine — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode). Unit tests run with `node --test` from this directory (`engine.test.ts` covers optimizer legality; `views.test.ts` is a seeded differential fuzz of view maintenance against fresh evaluation).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, join kinds, and ordering.

## NULL support

Values are JSON `null` for SQL NULL. Truth values are represented directly: TRUE is `true`, FALSE is `false`, UNKNOWN is `null`. WHERE, ON, and HAVING keep only `true`. Nullable columns, NULL literals, IS [NOT] NULL, COALESCE, LEFT [OUTER] JOIN, NULLS FIRST/LAST, and global SUM/MIN/MAX are all supported per the problem statement.

An untyped NULL literal has no type until its context demands one; the binder accepts it wherever an int, text, or bool is required. Every bound expression carries a conservative `nullable` flag derived from declared nullability, LEFT JOIN padding (the right table of a left join is nullable everywhere except inside its own ON clause), and aggregate emptiness (SUM/MIN/MAX are always nullable; COUNT never is).

## Optimizer

Constant folding is bottom-up and sound under three-valued logic: fully constant subexpressions are evaluated, NULL-strict operators with a NULL literal operand fold to NULL, FALSE absorbs AND and TRUE absorbs OR, TRUE/FALSE identities are dropped, double NOT collapses, and COALESCE drops NULL literals and everything after a non-nullable argument. The two-valued tautologies `x = x`, `p OR NOT p`, `p AND NOT p`, `x IS NULL`, and `x IS NOT NULL` fold only when the operand is provably non-nullable.

WHERE conjuncts that reference a single source are pushed into that source's scan when the source is never NULL-padded (the first table, or the right side of an inner join). For the padded side of a LEFT JOIN, a conjunct is pushed only when it is null-rejecting, tested by evaluating it against an all-NULL row; in that case the LEFT JOIN is reduced to an INNER JOIN first. Other conjuncts stay above the joins. ON conjuncts are never moved. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

## Checkpoint 2: incrementally maintained views

`views.ts` adds the `commands` request form. Base tables keep live rows by private ID together with a monotonic encounter position (updates keep their position, inserts take a fresh one, IDs are never reused) and a used-ID set. A batch is first shape-checked in full (`INVALID_COMMAND`), then every change is checked against a private overlay of the batch (`UNKNOWN_TABLE`, `INVALID_ROW`, `ROW_ID_USED`, `UNKNOWN_ROW`); only a fully valid batch touches base rows, ID sets, views, or the revision, so rollback is never needed. The net per-row effect of a batch (old row → new row) is what reaches views, so insert-then-delete is a no-op and rows changed on both join sides are retracted and re-added exactly once.

Each view owns a pipeline that mirrors the batch executor and is fed by the same bound (and, when requested, optimized) plan: per-source scans filtered by the plan's pushed-down predicates, a chain of join levels, and an output stage. A join level keeps its prefixes and its scan, the set of matched right positions per prefix, and, when the ON clause contains `left = right` equality conjuncts, hash indexes on both sides (NULL keys never match); other conjuncts are checked by evaluating the full ON expression. LEFT JOIN padding is inserted exactly when a prefix's last match is retracted and retracted when its first match arrives. Every derived row carries the encounter positions of its contributing base rows (0 for padding); nested-loop encounter order is lexicographic order of these keys, so ORDER BY ties, DISTINCT's first occurrence, and a group's first-encounter position are reconstructed from keys rather than from evaluation order.

The output stage applies WHERE, then either stores projected rows or maintains groups. Aggregates in SELECT/HAVING are rewritten to read slots appended after the source columns, and each group keeps COUNT/SUM running totals plus MIN/MAX values that are recomputed from the group's members only when the current extremum is deleted. Groups touched by a batch are refreshed (HAVING and projection) once at commit; empty grouped groups disappear while the global aggregate group persists. Reads sort, deduplicate and paginate the maintained result, cache it until the next change reaches the view, and return copies. Views over tables not touched by a batch are not visited.

Validation: all public baseline and extension cases pass in both optimizer modes; the differential fuzz compares 150 random schemas × 4 views × 12 batches against fresh evaluation. `starter.json` lists the export files. Build outputs and dependency installations are ignored.
