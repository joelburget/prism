# Query engine baseline — Prism

Prism 0.18.0 with its standard library and native toolchain. Run `./build.sh` once to compile `.build/query-null`; subsequent requests use the compiled executable.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into input scans when all joins are inner joins. Conjuncts referencing multiple sources remain above the join. Queries containing left joins retain WHERE predicates above the joins, because filtering a right input can create new NULL-padded rows. Folding uses the same three-valued evaluator as execution and preserves the bound expression type. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, NULL literals, three-valued boolean logic, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, explicit NULLS FIRST/LAST ordering, and grouped/global COUNT/SUM/MIN/MAX. JSON null represents SQL UNKNOWN in boolean expressions; predicates retain only true. The binder treats untyped NULL as compatible with the required context while checking every expression, including unreachable COALESCE arguments. Left joins pad unmatched right rows with NULL regardless of schema nullability. Aggregates ignore NULL arguments, with COUNT returning zero and SUM/MIN/MAX returning NULL when no non-null values remain.

Validation: all 21 public baseline and 34 public extension cases pass. Run `python3 starter/test_extension.py` from the workspace root after building for 26 additional scenarios in both optimizer modes, including empty right inputs, untyped NULL contexts, and errors on empty tables. This directory is self-contained. Build outputs and dependency installations are ignored.
