# Query engine — Python (checkpoint 2: incrementally maintained views)

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. A request carries either `queries` (checkpoint-one contract, unchanged) or `commands` (views); `main.py` dispatches on which field is present. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

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

## Incrementally maintained views (checkpoint 2)

`views.py` adds the `commands` mode. `Store` owns the mutable tables (per-table `live` dict of id -> row in encounter order, `used` dict of id -> sequence number, next sequence) and the named views. `View` binds and optionally optimizes the SQL with the unchanged parser/binder/optimizer and then maintains the result through a chain of `Stage` objects that mirror the engine's scan/join pipeline:

* Stage 0 is the scan of the first source (with any pushed-down filters); stage `s` joins the tuples of stage `s-1` with source `s`. Tuples are keyed by private row IDs (`None` marks LEFT JOIN padding) and carry their flattened row.
* Each stage indexes its outputs by prefix and by right row, counts TRUE matches per prefix (padding appears exactly when the count is zero), and hashes both sides on the equi-join conjuncts of ON (`x = y` with one side on earlier sources and the other on source `s`) so a changed row is only tested against candidate partners; the full ON expression is still evaluated on every candidate, so semantics are identical to the nested loop. NULL keys never enter an index because `NULL = x` is never TRUE.
* A batch delta is applied as retractions first (vanished prefixes, vanished right rows) and then additions (new right rows against surviving prefixes, then new prefixes against the whole right side), so a pair changed on both sides is produced exactly once. Per-stage events are netted (`net`) so a tuple padded and unpadded within one batch is not propagated, while a tuple whose values changed is propagated as retract + insert.
* Above the joins, WHERE keeps TRUE rows. Non-aggregate views store one projected row per tuple with its encounter key (the tuple of per-table sequence numbers, which updates keep and inserts extend). Aggregate views store per group COUNT/SUM counters and MIN/MAX multisets (`Counter`); only deleting the current extreme rescans that group's remaining values. Groups remember their earliest encounter key so group order matches a fresh query; a group whose member count hits zero is dropped, except the global group. Group projection/HAVING is recomputed only for groups dirtied since the last read, using the engine's evaluator with a mapping of maintained aggregate values.
* DISTINCT, ORDER BY and LIMIT/OFFSET run over the maintained result on read, and the rendered rows are cached until the next batch that touches one of the view's sources. Reads return fresh lists.

Batches are validated (shape for all changes, then per-change table/row/ID checks in order) against private copies of the touched tables' `live`/`used` state; only a fully valid batch commits, after which the net per-table delta (old row vs new row per id, so insert-then-delete and no-op updates vanish) is pushed to the views whose sources changed and the revision advances. Create/read/drop never advance the revision.

Validation: all 55 public baseline and 20 public extension cases pass. `test_views.py` checks command shapes, envelope rules and snapshot immutability, then runs randomized differential rounds that compare every view read after every batch with a fresh checkpoint-one query on the same final table contents (joins, LEFT joins, self-joins, grouping, HAVING, DISTINCT, ORDER BY, LIMIT/OFFSET, both optimizer modes).
