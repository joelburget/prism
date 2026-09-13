# Query engine — Python (NULL and left outer joins)

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models (`model.py`), SQL tokenization/parsing (`parser.py`), binding and type checking (`binder.py`), and execution/optimization (`engine.py`). Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, join kinds, and ordering.

## NULL semantics

SQL NULL is Python `None`, so UNKNOWN is simply a `None` boolean; `engine.evaluate` implements three-valued AND/OR/NOT and NULL propagation through arithmetic and comparison explicitly, and `engine.true` is the single place where WHERE/ON/HAVING keep TRUE only. An untyped NULL literal has the pseudo-type `null`, which unifies with the type its context requires; `COALESCE` needs one common concrete type across its non-NULL arguments. Binding also records conservative *nullability* per expression: a column is nullable when the schema says so or when it comes from the padded side of a LEFT JOIN, and the property is propagated through operators (IS [NOT] NULL and COUNT are never NULL; SUM/MIN/MAX always may be).

Left joins emit every ON-matching pair and pad unmatched left rows once with NULL across the whole right schema, taken from the declared column count so empty right tables still pad. Group keys treat NULL as a single key per position, DISTINCT treats NULLs as equal, and ORDER BY partitions NULLs out of the sort so placement is independent of direction (default LAST for ASC, FIRST for DESC, overridden by an explicit NULLS clause). Global SUM/MIN/MAX return NULL for an empty or all-NULL group while COUNT returns zero.

## Optimization

`engine.optimize` keeps both modes semantically identical, including output order:

* Bottom-up constant folding evaluates literal-only subtrees with the same three-valued evaluator, so `FALSE AND NULL` folds to FALSE while `NULL = NULL` folds to NULL.
* Algebraic rules that are only valid in two-valued logic are guarded by the recorded nullability: `x = x`, `p OR NOT p`, `p AND NOT p`, and `IS [NOT] NULL` fold only when the operand can never be NULL, which excludes nullable columns and everything on the padded side of an outer join. The identity/annihilator rules for TRUE/FALSE hold in three-valued logic and are applied unconditionally.
* Single-source WHERE conjuncts are pushed into the scan of their source when that is provably safe: into the first source always, into the right input of an inner join always, and into the right input of a left join only when the conjunct is null-rejecting — then the padded rows would be discarded anyway and the join is rewritten to an inner join. Predicates over several sources, and non-null-rejecting predicates such as `r.v IS NULL` or `COALESCE(r.v, 0) < 15`, stay above the join. ON clauses are folded but never pushed.

Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

## Tests

All 21 public baseline and 34 public extension cases pass (`python3 run.py run --task query-null --command './starter/run.sh'` from the bundle root). `python3 tests.py` runs an additional agent-authored suite: NULL edge cases, diagnostics, schema validation, both optimizer modes compared against explicit expectations, assertions that the optimizer actually folds and pushes filters (and refuses to when unsound), and an end-to-end subprocess check of the wire protocol.

This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
