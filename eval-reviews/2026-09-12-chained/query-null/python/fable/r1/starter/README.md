# Query engine baseline — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions and pushes single-source WHERE conjuncts into inner-join input scans. Conjuncts referencing multiple sources remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

Supported: SELECT/WHERE/inner and left outer joins, grouped and global COUNT/SUM/MIN/MAX, GROUP BY/HAVING, DISTINCT/ORDER BY/LIMIT/OFFSET, nullable columns, NULL literals, IS [NOT] NULL, COALESCE, and explicit NULLS FIRST/LAST.

## NULL and three-valued logic

SQL NULL is Python `None` end to end. Predicates evaluate to `True`, `False`, or `None` (UNKNOWN); `engine.is_true` is the only truth test used by scans, joins, WHERE, and HAVING, so FALSE and UNKNOWN are discarded uniformly. Untyped NULL literals carry the type `'null'`, which the binder unifies with whatever type the surrounding operator requires (COALESCE arguments and comparison operands must otherwise agree). Every bound expression also gets a conservative `nullable` flag: declared nullability, NULL literals, aggregates other than COUNT, and any column on the null-supplied side of a LEFT JOIN are nullable, and the flag propagates through operators (COALESCE is nullable only if every argument is).

LEFT JOIN emits one NULL-padded row for each left row with no TRUE match. Group keys treat NULLs as equal per position, DISTINCT compares whole rows with NULLs equal, and sorting places NULLs LAST for ASC and FIRST for DESC unless an explicit NULLS clause overrides it.

## Optimizer legality

Constant folding evaluates all-literal subexpressions with the same three-valued evaluator (so `NULL = NULL` folds to NULL, `FALSE AND NULL` to FALSE). Rewrites that are only valid for non-null operands (`x = x`, `x IS NULL`, `x <> x`) consult the `nullable` flag and are skipped for nullable columns and outer-join outputs. `TRUE AND p`/`FALSE OR p` reduce to `p`; the absorbing literal wins for `FALSE AND p`/`TRUE OR p`, which holds for UNKNOWN `p` too. `p OR NOT p` is never folded.

Single-source WHERE conjuncts are pushed into a table scan only when that table is never NULL-padded (the first table, or the right side of an INNER JOIN). A conjunct on the null-supplied side of a LEFT JOIN is first tested for null rejection by evaluating it on an all-NULL row: if it cannot be TRUE there, the padded rows would be discarded anyway, so the join is converted to INNER and the conjunct is pushed. Otherwise it stays above the join. WHERE conjuncts are never moved into ON.

Validation: all 21 public baseline and 34 public extension cases pass in both optimizer modes. `test_null.py` adds fixed edge cases and a randomized differential test comparing optimized and unoptimized execution. This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
