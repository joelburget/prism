# Query engine — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into the input scan of any source that cannot be NULL-padded at that point in the join chain (the leftmost source, and any source introduced by an INNER join). Conjuncts referencing multiple sources, or a source introduced by a LEFT JOIN, remain above the join so that outer-join padding semantics stay intact. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

This implementation supports nullable schemas, SQL NULL and three-valued (TRUE/FALSE/UNKNOWN) logic, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN alongside INNER JOIN, explicit ORDER BY NULLS FIRST/LAST, and global (ungrouped) SUM/MIN/MAX including empty input, in addition to the non-null baseline (SELECT/WHERE/inner joins, grouped COUNT/SUM/MIN/MAX, global COUNT, GROUP BY/HAVING, DISTINCT/ORDER BY/LIMIT/OFFSET).

Expression types are one of `int`, `text`, `bool`, or the untyped `null` (assignable to whatever type its context requires). NULL propagates through arithmetic and comparisons, AND/OR/NOT follow the standard three-valued truth tables, GROUP BY and DISTINCT treat NULL as equal to itself, and LEFT JOIN pads every right-side column with NULL when no match is found, per the task's defined evaluation behavior.

Validation: all 21 public baseline cases and all 34 public extension cases pass. This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
