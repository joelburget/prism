# Query engine baseline — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas and SQL NULL throughout evaluation: three-valued boolean logic, IS NULL/IS NOT NULL, COALESCE, null-aware aggregates and DISTINCT, and default or explicit NULLS ordering. LEFT [OUTER] JOIN emits null-padded unmatched rows. Global COUNT/SUM/MIN/MAX produce one row even on empty input. Optimized plans retain constant folding and restrict filter pushdown to all-inner-join plans, where it remains valid under NULL semantics.

This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
