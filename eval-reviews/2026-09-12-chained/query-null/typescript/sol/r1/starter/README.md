# Query engine baseline — Typescript

Node.js 25+ executes the erasable TypeScript directly. For static validation, run `npm ci --ignore-scripts` then `npm run typecheck` (TypeScript 5.9.3, strict mode).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, SQL three-valued logic, NULL/IS NULL/COALESCE, left outer joins, explicit NULL ordering, and global nullable aggregates. Optimized execution folds constant nullable expressions and retains safe scan-filter pushdown for all-inner join plans without moving predicates across outer joins.

This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.

Checkpoint two adds transactional `create`, `read`, `drop`, and `apply` commands.
Materialized views retain provenance-bearing join stages and per-group membership;
successful batches retract and add only affected join branches and aggregate
groups. Base-row IDs and encounter positions are maintained independently of SQL
columns, and a batch is validated against a private state before it is committed.
