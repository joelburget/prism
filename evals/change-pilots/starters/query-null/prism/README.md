# Query engine baseline — Prism

Prism 0.22.0 with its standard library and native toolchain. Run `./build.sh` once to compile `.build/query-null`; subsequent requests use the compiled executable.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

This is the non-null baseline: SELECT/WHERE/inner joins, grouped COUNT/SUM/MIN/MAX, global COUNT, GROUP BY/HAVING, DISTINCT/ORDER BY/LIMIT/OFFSET. Nullable schemas, NULL/IS NULL/COALESCE, LEFT JOIN, explicit NULLS ordering, and global SUM/MIN/MAX are intentionally rejected with `UNSUPPORTED_FEATURE`. The starter does not implement the requested extension.

Validation: all 21 public baseline cases pass. The extension suite is a future modification target, not a conformance claim. This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
