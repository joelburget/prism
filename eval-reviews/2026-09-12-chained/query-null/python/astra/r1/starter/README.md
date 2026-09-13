# Query engine — Python

Python 3.10+ (validated with Python 3.14). No dependencies.

Run `./run.sh`. It reads one protocol-v1 `query-null` JSON request from stdin and writes one JSON response. Every request starts a fresh process. Domain errors exit zero and contain only the fixed error code. No SQL library or external query engine is used.

The modules separate wire validation and models, SQL tokenization/parsing, binding and type checking, and execution/optimization. Binding resolves columns to flattened row slots and verifies all expressions before evaluating rows, including unreachable expressions and empty tables. The logical plan carries typed expressions, input tables, scan predicates, and ordering.

Optimization performs bottom-up folding of constant scalar expressions using three-valued logic. Single-source WHERE conjuncts can move into scans of the initial source and sources introduced by inner joins. Filters on sources introduced by left joins remain above the joins, because filtering a scan could create new unmatched rows. Conjuncts referencing multiple sources also remain above the join. Stable nested-loop joins, encounter-ordered groups, first-occurrence DISTINCT, stable sorting, and pagination preserve the documented ordering in either mode.

The engine supports nullable schemas, NULL literals, three-valued scalar expressions, IS NULL/IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, and explicit NULLS FIRST/LAST. Python `None` represents SQL NULL, including UNKNOWN boolean results; predicates retain only `True`. COUNT(expr) ignores NULLs, and global or grouped SUM/MIN/MAX ignore NULLs and return NULL when no values remain. Binding tracks nullability introduced by left joins and resolves contextual types for NULL expressions before optimization, including unreachable branches.

Validation: all 55 checkpoint-one baseline and 20 checkpoint-two extension cases pass. Additional regressions and optimizer checks run with `python3 -m unittest discover -s starter` from the parent directory. This directory is self-contained; `starter.json` lists its export files. No build step is needed. Build outputs and dependency installations are ignored.


Checkpoint two adds the `database`/`commands` input form, named views, atomic
insert/update/delete batches, private row IDs, and global revisions. `views.py`
reuses the parser, binder, optimizer, and scalar evaluator. Command validation
stages only touched row IDs and ID reservations before committing; all expected
domain errors occur before any live state is changed. Reads use cached results.
The original `database`/`queries` execution path and launch entrypoint remain intact.

Each source has monotonic encounter tokens independent of user row IDs. Join
stages retain both inputs, equality-conjunct indexes, and output contributions
per left lineage. A right delta selects affected left lineages using old and new
keys; a stage installs both input deltas before replacing their contributions.
General predicates without usable equality conjuncts inspect all left candidates.
NULL padding is represented by a zero lineage token. Final lineage order restores
nested-loop encounter order regardless of index traversal order.

Groups retain bag contributions, non-null value counts, running COUNT/SUM state,
and an encounter-order heap. Retractions update only affected groups; MIN/MAX
inspect those groups' remaining distinct values. Projection is cached per row or
group, and DISTINCT, sorting and pagination may revisit the cached output.
Unrelated views skip maintenance entirely. New result lists preserve earlier
read snapshots; updates never mutate stored row values in place.

Verification for checkpoint two:

- Acceptance: `python3 run.py run --task query-null --command './starter/run.sh'`
  passes baseline 55/55 and extension 20/20.
- Additional tests: `python3 -m unittest discover -s starter` passes 9 tests,
  including 1,680 randomized view/read comparisons with fresh execution across
  12 SQL queries in both optimizer modes, plus rollback and snapshot checks.
- Review effort: one manual implementation review of insert/retract paths,
  simultaneous join-side deltas, group order, snapshot ownership, validation
  precedence, ID reservations, and unaffected-view skipping. This is separate
  from behavioral acceptance and timing measurements.
- Scaling: no supplied scaling harness is present in this checkout. The local
  `python3 starter/benchmark_views.py` smoke workload uses three views and 300
  update/read pairs. Observed times were 0.216s for 4,000 initial rows and 1.049s
  for 16,000 initial rows, including view creation. These are uncalibrated local
  measurements, not scaling acceptance thresholds.
