# Query engine — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions using three-valued logic. Single-source WHERE conjuncts can move into scans of the initial source and sources introduced by inner joins. Filters on sources introduced by left joins remain above the joins, because filtering a scan could create new unmatched rows. Conjuncts referencing multiple sources also remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, NULL literals, three-valued scalar expressions, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, and explicit NULLS FIRST/LAST. Python `None` represents SQL NULL, including UNKNOWN boolean results; predicates retain only `True`. COUNT(expr) ignores NULLs, and global or grouped SUM/MIN/MAX ignore NULLs and return NULL when no values remain. Binding tracks nullability introduced by left joins and resolves contextual types for NULL expressions before optimization, including unreachable branches.

Validation: all 21 public baseline and 34 public extension cases pass. Additional regressions and optimizer checks run with `python3 -m unittest discover -s starter` from the parent directory. This directory is self-contained; `starter.json` lists its export files. No build step is needed. Build outputs and dependency installations are ignored.
