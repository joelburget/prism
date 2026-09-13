# Query engine baseline — Prism

Prism 0.18.0 with its standard library and native toolchain. Run `./build.sh` once to compile `.build/query-null`; subsequent requests use the compiled executable.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, SQL three-valued logic, NULL/IS NULL/COALESCE,
left outer joins, explicit NULL ordering, and null-aware grouped and global
aggregates. Optimized execution retains constant folding and pushes single-source
filters only when doing so is safe with respect to preceding outer joins.

Checkpoint-two command requests keep private row IDs and never expose them to SQL.
Each command produces a new immutable store value, so a failed apply naturally
discards all tentative rows, used-ID reservations, and view changes. Views own a
bound (and optionally optimized) plan plus a materialized result. Successful apply
batches refresh only views whose recorded base-table dependencies changed; reads
return the cached snapshot and merely attach the current global revision. Plan
refresh preserves bound expressions and optimizer decisions while replacing the
changed table snapshots, and performs maintenance once against the final state of
a multi-table batch.

This directory is self-contained; `starter.json` lists its export files. Build
outputs and dependency installations are ignored.
