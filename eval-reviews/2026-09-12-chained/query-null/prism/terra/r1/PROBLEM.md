# Checkpoint 2: incrementally maintained query views

Extend the completed **Query engine: NULL and left outer joins** from checkpoint 1.
The complete earlier contract is supplied in `PREVIOUS.md`. Every old request and
response, including errors, optimizer behavior and output ordering, remains valid.
Keep the existing query engine and extend it; do not call another SQL engine.
This checkpoint is revealed only after the first submission is frozen.

## Requested change

Maintain named query views as multiple base tables change atomically. Reuse the
existing parser, binder, expressions, optimizer and query semantics. Support the
entire checkpoint-one SQL subset in views, including chained inner/left joins,
nullable expressions, grouping, DISTINCT, sorting and LIMIT/OFFSET. Both optimizer
settings remain supported. Newly created views immediately include existing rows.

Implement incremental maintenance: retain useful state between changes, avoid
re-evaluating unaffected views, and maintain joins and aggregates from affected
rows/groups. Computing every view from scratch on every change or read is not an
acceptable completed implementation. Initial view creation may perform a full
query. MIN/MAX deletions may revisit the affected group, and sorting/output may
visit the view result. No internal representation is prescribed. The behavioral
suite cannot prove this algorithmic requirement; it is separately reviewed and
measured with the supplied scaling workloads. Never infer acceptance from a
self-reported work counter.

## Protocol and compatibility

Use the same envelope and `task:"query-null"`. Inputs with `queries` follow the
old contract exactly. The new input form has exactly `database` and `commands`.
Mixing `queries` and `commands`, omitting either new field, or supplying a non-array
commands value is `INVALID_INPUT`. Validate the complete database using the old
rules before processing commands. Empty databases and command arrays are allowed.
There are at most 2,000 commands, 16 tables, 20,000 initial rows across tables and
20,000 live rows across tables. Behavioral fixtures respect these resource bounds;
resource-bound violations other than the command limit are outside scope.

A new-mode response is `{"ok":true,"result":{"results":[REPLY,...]}}`, one reply per
command in order. Each reply is `{"ok":true,"result":VALUE}` or
`{"ok":false,"error":{"code":CODE}}`. A command error changes no state and processing
continues. Earlier replies are immutable snapshots. Top-level validation errors
retain the old error envelope. Numbers remain exact integers within the earlier
input/intermediate bounds; row identifiers and revisions have separate bounds below.

Initial rows have private identifiers 1..N **per table**, in input order. These IDs
are not SQL columns and cannot be referenced from SQL. An update preserves a row's
encounter position; deleting removes it; inserting appends it. A row ID can never
be reused in that table after a successful insertion, including after deletion.
Failed commands do not consume IDs. IDs may be equal across different tables.

## Commands

Objects have exactly the fields shown. View names match `[a-z][a-z0-9-]{0,39}`;
SQL identifiers continue to use the earlier grammar. At most 32 simultaneous views
are used. Command-shape errors are `INVALID_COMMAND`.

* `{"op":"create","view":"v","sql":"SELECT ...","optimize":true}`:
  validate the field shapes, reject an existing view with `VIEW_EXISTS`, then parse
  and bind the query using the earlier errors. On success install its result and
  return `{"view":"v","revision":R}`. Views query base tables, not other views.
* `{"op":"read","view":"v"}`: return
  `{"revision":R,"columns":[...],"rows":[...]}` using the old query result rules.
  An unknown view is `UNKNOWN_VIEW`. R is the global database revision, even when
  this view was unaffected by recent updates.
* `{"op":"drop","view":"v"}`: remove it and return `{"dropped":"v"}`; an unknown
  view is `UNKNOWN_VIEW`. The name may subsequently be reused for a different SQL
  query. Creating, reading and dropping do not advance the database revision.
* `{"op":"apply","changes":[CHANGE,...]}`: atomically apply a nonempty array of at
  most 200 changes, advance revision by one, and return `{"revision":R}`. Start at
  revision zero. Every successful batch advances it, including a batch whose final
  table contents equal the initial contents. Rejected batches never advance it.

Each CHANGE has one of these exact forms:

```json
{"op":"insert","table":"items","id":3,"row":[3,20]}
{"op":"update","table":"items","id":2,"row":[2,null]}
{"op":"delete","table":"items","id":1}
```

IDs are integers 1..2,147,483,647, not booleans. Table fields are strings matching `[a-z_][a-z0-9_]*`. A matching name absent
from the database is UNKNOWN_TABLE, including a reserved SQL word. Insert/update `row` must be an array. Validate shapes and scalar
ID/name constraints for **all** changes before applying any: failure is
`INVALID_COMMAND`. Then process changes in array order against a private batch
state. For each change, check table existence (`UNKNOWN_TABLE`); for insert/update,
check width, type, nullability and numeric bounds (`INVALID_ROW`); then check ID
state (`ROW_ID_USED` for inserting any previously used ID; `UNKNOWN_ROW` for
updating/deleting an absent row). Updates can change every SQL-visible column.

The batch commits all base rows, used-ID sets, view states and the revision
simultaneously. If any change fails, roll everything back. Multiple changes may
address the same row, including insert-then-update or insert-then-delete. A
successful insert-then-delete still reserves its ID. Views see the final batch
state, with exactly the result a fresh query would have at that point. Do not
emit intermediate view results. Joined rows changed on both sides in one batch
must not be lost or counted twice.

## Interactions that must work

* Removing a left row's last matching right row creates one NULL-extended row;
  adding its first match removes that placeholder. Multiple matches retain bag
  multiplicity. NULL does not match NULL with ordinary equality.
* Base duplicates and duplicate projected rows are distinct contributions before
  DISTINCT. Removing one occurrence need not remove the result row or group.
* Changing a join/group key retracts the old contribution and adds the new one.
  WHERE and HAVING may move a row/group into or out of the result. GROUP BY merges
  NULL keys; aggregates ignore NULL values as specified previously.
* Deleting the final member removes a grouped group. A global aggregate continues
  to produce one row on empty input. COUNT(*) differs from COUNT(nullable_expr).
* Deleting a minimum/maximum, moving an ORDER BY key, or crossing a LIMIT/OFFSET
  boundary must reveal the correct replacement row. Tie order is the original
  deterministic encounter order, including after updates.
* Failed batches and dropped/recreated views must not leave stale indexes or
  aggregate contributions. Reads must not change results or leak mutable aliases.

## Example

For a one-column integer table `t` initially containing `[[4],[4]]`, create
`SELECT DISTINCT x AS x FROM t ORDER BY x` as `v`. Reading v yields `[[4]]` at
revision 0. Deleting row ID 1 yields the same rows at revision 1. Deleting ID 2
then yields `[]` at revision 2. A global `COUNT(*)` view instead yields `[[0]]`.

## Acceptance and review

All checkpoint-one public cases are now public baseline regressions; all its
held-out cases remain private baseline regressions. New public extension tests
illustrate the protocol. Additional unseen update sequences use these same rules.
Run `python3 run.py run --task query-null --command './starter/run.sh'`.
Review the patch for coherent insert/retract paths, ownership of cached state,
bag multiplicities, batch atomicity, and independent optimized/unoptimized behavior.
Record review effort separately from acceptance and scaling results. Scaling
workloads measure this implementation; their thresholds require toolchain-specific
calibration and are not a hidden wall-clock correctness requirement.
