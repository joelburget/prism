# Query engine baseline — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, SQL three-valued logic, NULL/IS NULL/COALESCE, LEFT OUTER JOIN, explicit NULLS ordering, and global nullable aggregates. Optimizer folding evaluates constants with the same NULL semantics as runtime execution, while filter pushdown remains active for all-inner-join plans and is withheld when an outer join could change its meaning.

The checkpoint-two command protocol adds named materialized views and atomic insert/update/delete batches. Views cache joined contributions by private base-row ID and cache projected rows or aggregate-group members. A batch clones only dependent views, retracts combinations involving changed rows or affected outer-join placeholders, adds final-state combinations, and recomputes only touched aggregate groups. Unaffected views are not evaluated, and reads format immutable cached contributions through DISTINCT, ordering, and pagination.

This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
