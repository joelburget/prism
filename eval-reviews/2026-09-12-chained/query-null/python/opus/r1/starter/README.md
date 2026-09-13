# Query engine — Python (NULL, left outer joins, incremental views)

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

`input` carries either `queries` (checkpoint one: one batch result per query) or `commands` (checkpoint two: one reply per command over incrementally maintained views). The two forms are mutually exclusive and the database is validated with the same rules in both.

The modules separate wire validation and models (`model.py`), SQL tokenization/parsing (`parser.py`), binding and type checking (`binder.py`), execution/optimization (`engine.py`), the command protocol and mutable base tables (`session.py`), and incremental view maintenance (`views.py`). Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, join kinds, and ordering.

## NULL semantics

SQL NULL is Python `None`, so UNKNOWN is simply a `None` boolean; `engine.evaluate` implements three-valued AND/OR/NOT and NULL propagation through arithmetic and comparison explicitly, and `engine.true` is the single place where WHERE/ON/HAVING keep TRUE only. An untyped NULL literal has the pseudo-type `null`, which unifies with the type its context requires; `COALESCE` needs one common concrete type across its non-NULL arguments. Binding also records conservative *nullability* per expression: a column is nullable when the schema says so or when it comes from the padded side of a LEFT JOIN, and the property is propagated through operators (IS [NOT] NULL and COUNT are never NULL; SUM/MIN/MAX always may be).

Left joins emit every ON-matching pair and pad unmatched left rows once with NULL across the whole right schema, taken from the declared column count so empty right tables still pad. Group keys treat NULL as a single key per position, DISTINCT treats NULLs as equal, and ORDER BY partitions NULLs out of the sort so placement is independent of direction (default LAST for ASC, FIRST for DESC, overridden by an explicit NULLS clause). Global SUM/MIN/MAX return NULL for an empty or all-NULL group while COUNT returns zero.

## Optimization

`engine.optimize` keeps both modes semantically identical, including output order:

* Bottom-up constant folding evaluates literal-only subtrees with the same three-valued evaluator, so `FALSE AND NULL` folds to FALSE while `NULL = NULL` folds to NULL.
* Algebraic rules that are only valid in two-valued logic are guarded by the recorded nullability: `x = x`, `p OR NOT p`, `p AND NOT p`, and `IS [NOT] NULL` fold only when the operand can never be NULL, which excludes nullable columns and everything on the padded side of an outer join. The identity/annihilator rules for TRUE/FALSE hold in three-valued logic and are applied unconditionally.
* Single-source WHERE conjuncts are pushed into the scan of their source when that is provably safe: into the first source always, into the right input of an inner join always, and into the right input of a left join only when the conjunct is null-rejecting — then the padded rows would be discarded anyway and the join is rewritten to an inner join. Predicates over several sources, and non-null-rejecting predicates such as `r.v IS NULL` or `COALESCE(r.v, 0) < 15`, stay above the join. ON clauses are folded but never pushed.

Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

## Incremental views

`session.py` owns state: one `Table` per base table holding `id -> (seq, row)`, the set of identifiers ever used, and the next sequence number. `seq` is a private encounter number — initial rows take 1..N, an insert appends the next one, an update keeps the row's own — so it expresses the "updates preserve position, inserts append" rule without re-numbering anything. A batch is applied straight to those tables while recording the pre-image of every touched `(table, id)`, the identifiers it reserved, and each table's previous `next_seq`; a failing change restores all three and no view is touched, so rejected batches cannot advance the revision or leave partial state. On commit the pre-images are folded into one net delta per table (insert, delete, or a delete/insert pair at the same `seq`), which is what the views see — an insert-then-delete in the same batch reaches them as nothing at all while still burning its identifier.

`views.py` wires the *same bound `Plan`* the batch engine executes into a dataflow of operators that consume row deltas. Every intermediate row carries a **provenance key**: the tuple of per-source sequence numbers it was built from, with `0` marking the NULL-padded right side of a left join. Because sequence numbers follow encounter order, the lexicographic order of provenance keys is exactly the nested-loop order the contract prescribes, so each operator can hold an unordered dict keyed by provenance and still reproduce a deterministic result. Ownership is strict: an operator only mutates its own `rows` dict and indexes, and reads its inputs' dicts.

* `Source` applies the optimizer's pushed scan predicates, so both optimizer modes share one incremental path.
* `JoinNode` maintains one join step symmetrically. A new row on either side probes the other and emits matching pairs; a retraction drops exactly the output rows recorded for that side in `by_left`/`by_right`. A left join also counts matches per left row, so losing the last match materialises the padded row and gaining the first retracts it. Duplicate matches are separate provenance keys, so bag multiplicity is preserved. When the ON clause contains `left expression = right expression` conjuncts, both sides are hash-indexed on that key and only candidates are tested — sound because ON is a conjunction, so it can only be TRUE when that equality is TRUE, which requires two equal non-NULL values (a NULL key simply probes nothing). Other ON clauses fall back to scanning the opposite side.
* `Sink` applies WHERE and then either projects directly into a `key -> row` map or maintains groups. A group keeps its members, one `Accumulator` per aggregate node, and its first (smallest) provenance key. COUNT/SUM are running totals; MIN/MAX keep a value multiset and a cached extreme, and only deleting that extreme forces a revisit of the group — likewise deleting a group's first member forces its position to be recovered. Only touched groups are re-projected, through the shared expression evaluator with the finished aggregate values supplied as `engine.Slots`. A group vanishes with its last member, except the single group of a global aggregate, which always yields its row.
* Reads sort the maintained result by provenance and hand it to `engine.finish`, the same DISTINCT / ORDER BY / OFFSET / LIMIT stage batch execution uses, and copy every row out, so a read cannot change or alias view state.

A change therefore costs work proportional to the affected rows and groups rather than to the database: views over untouched tables do nothing, and dropping a view discards all of its indexes and accumulators with it.

## Tests

All 55 public baseline and 20 public extension cases pass (`python3 run.py run --task query-null --command './starter/run.sh'` from the bundle root). `python3 tests.py` runs an additional agent-authored suite: NULL edge cases, diagnostics, schema validation, both optimizer modes compared against explicit expectations, assertions that the optimizer actually folds and pushes filters (and refuses to when unsound), the command protocol (shapes, view lifecycle, identifier reuse, batch atomicity and rollback), a replay that compares every incrementally maintained view against a fresh batch query after each batch in both optimizer modes, and end-to-end subprocess checks of both wire forms.

This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
