# Query engine — Prism

Prism 0.18.0 with its standard library and native toolchain. Run `./build.sh` once to compile `.build/query-null`; subsequent requests use the compiled executable. `build.sh` routes the native link through `link.sh`, which bundles the compiler's per-function objects into one archive so the link succeeds under a small open-file limit (the compiler otherwise hands the system linker several hundred objects at once).

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models (`Model.pr`), SQL tokenization/parsing (`Parser.pr`), binding and type checking (`Binder.pr`), and execution/optimization (`Engine.pr`). Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, per-join outer flags, scan predicates, and ordering.

## NULL support

SQL NULL is `JNull` everywhere: in table data, in the untyped `NULL` literal (type `null`, which adopts the type its context requires), and as the UNKNOWN truth value of a boolean expression. `WHERE`, `HAVING`, and `ON` retain a row only when the predicate is exactly `JBool(true)`. Arithmetic and ordinary comparison propagate NULL, `NOT` maps NULL to NULL, and `AND`/`OR` follow the three-valued truth tables. `IS NULL`/`IS NOT NULL` always yield a boolean; `COALESCE` returns the first non-null argument.

Every bound expression carries a static `nullable` upper bound. A column is nullable when its schema says so or when its table is the right side of a `LEFT JOIN`, whose unmatched rows are padded with NULL regardless of declaration. `COUNT` and `IS [NOT] NULL` are never null; `SUM`/`MIN`/`MAX` may be (empty or all-NULL input); other operators are nullable when any operand is.

`LEFT [OUTER] JOIN` emits one NULL-padded row for each left row without a TRUE match; a NULL key never equals anything. Group keys and `DISTINCT` compare NULLs as equal through their JSON encoding. Aggregates other than `COUNT(*)` ignore NULL; `SUM`/`MIN`/`MAX` return NULL when nothing remains, and a global aggregate over empty input yields exactly one group. `ORDER BY` supports `NULLS FIRST`/`NULLS LAST`, defaulting to last for `ASC` and first for `DESC`.

## Optimizer

`optimize` performs bottom-up simplification and filter pushdown; both modes must produce identical output.

* Literal-only subtrees are evaluated with the same three-valued evaluator used at run time, so `NULL = NULL` folds to NULL and `FALSE AND NULL` to FALSE. Expressions over columns (`x = x`, `p OR NOT p`) are never folded.
* `e IS NULL` folds to FALSE and `e IS NOT NULL` to TRUE only when the binder proved `e` non-nullable, which already accounts for outer-join padding. `COALESCE(e, ...)` reduces to `e` when `e` is non-nullable.
* A WHERE conjunct referencing a single join input is pushed into that input's scan when the input is an inner-join side or the preserved (left) side of a left join. For the padded (right) side of a left join it is pushed only if it is null-rejecting, i.e. it cannot be TRUE on an all-NULL row of that input; in that case the join is first rewritten to an inner join, which is equivalent because padded rows could never pass the filter. Other conjuncts stay above the joins. ON predicates are never moved.

## Tests

All 55 public cases (21 baseline, 34 extension) pass in both optimizer modes. `test_fuzz.py` is a differential fuzz test: it generates random nullable databases and queries, evaluates them with an independent Python oracle written from the contract, and checks the engine in both modes (`python3 test_fuzz.py --seed 1 --count 40`). This directory is self-contained; `starter.json` lists its export files. Build outputs and dependency installations are ignored.
