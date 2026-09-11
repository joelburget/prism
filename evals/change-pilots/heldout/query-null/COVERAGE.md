# Held-out query-engine coverage

These 28 evaluator-side cases add combinations and binder boundaries to the
published contract in `../../query-null/PROBLEM.md`. They introduce no new SQL
features or requirements. Five are baseline regressions and 23 exercise the
extension. IDs are disjoint from public cases. Every successful request runs
the same query with optimization disabled and enabled.

The table compares each case with the public corpus as it existed when these
cases were added. The proposed faulty behaviors are review hypotheses, not
measured mutation-test results. Neither these cases nor the public suite prove
that an implementation actually optimizes queries.

| Case | Phase | Additional coverage versus public cases | Plausible faulty generalization |
| --- | --- | --- | --- |
| `heldout-aliased-self-inner-join` | baseline | Public inner joins use two different tables, never two bindings of one table. | Keying scan bindings by physical table name confuses the self-join aliases. |
| `heldout-group-keys-without-aggregates` | baseline | Public grouped cases all contain aggregate functions; no expression over a key without aggregates. | Treating GROUP BY as active only when SELECT includes an aggregate emits duplicates or skips HAVING. |
| `heldout-empty-inner-join-global-count` | baseline | Public empty COUNT scans an empty table directly, never an empty join intermediate. | Dropping an empty intermediate plan before the aggregate suppresses its required zero-count output. |
| `heldout-alias-hides-original-table` | baseline | Public alias use is valid; no attempt uses the original qualifier after aliasing. | Keeping both the old table name and alias in the binder incorrectly accepts an unavailable qualifier. |
| `heldout-duplicate-source-qualifier` | baseline | Public duplicate aliases concern SELECT output names, not source bindings. | Checking only output-alias uniqueness silently overwrites one source binding. |
| `heldout-empty-left-global-aggregation` | extension | Public LEFT JOIN always has nonempty left input, and empty global aggregation is tested only on a scan. | Confusing LEFT JOIN with a symmetric outer join creates right-side rows or loses the global empty group. |
| `heldout-both-empty-left-grouped` | extension | Public empty grouped aggregation does not follow an outer join. | Synthesizing a NULL group for every empty outer join yields a spurious row. |
| `heldout-true-or-unknown-on` | extension | Public truth tables are projected; no OR expression drives outer-join matching. | Treating the presence of any UNKNOWN operand as an UNKNOWN predicate drops valid matches. |
| `heldout-false-and-unknown-on` | extension | Public ON examples use key equality and numeric filters, without a constant-folded nullable boolean conjunction. | Turning UNKNOWN into a match or padding once per rejected right row changes multiplicity. |
| `heldout-explicit-null-safe-join` | extension | Public NULL-key join checks that ordinary equality does not match NULL; no explicit null-safe predicate. | Unconditionally discarding NULL join keys before evaluating the full ON expression loses intended matches. |
| `heldout-nullable-self-left-join` | extension | Public chained joins use distinct tables; they do not combine self-join aliases, nullable references, and fallback projection. | Sharing a table cursor or null-padding the entire physical table destroys the preserved alias. |
| `heldout-coalesce-rematches-padded-join` | extension | Public chained LEFT JOIN uses plain equality, so padded rows remain unmatched at the next stage. | Assuming once-unmatched rows cannot match later joins skips the fallback-key match. |
| `heldout-where-disjunction-preserves-padding` | extension | Public WHERE tests isolate numeric rejection and IS NULL; no disjunction combines matched and unmatched rows. | Pushing the full WHERE predicate into ON would wrongly preserve left id 2 as a new padded row. |
| `heldout-distinct-null-ties-pagination` | extension | Public DISTINCT NULL, sorting, and pagination tests are separate; this joins all three with NULL tie stability. | Deduplicating by the first projection only merges different NULL rows; sorting ties by hidden id also changes the page. |
| `heldout-count-predicate-counts-false` | extension | Public COUNT only consumes columns, never a nullable boolean expression. | Implementing COUNT(expr) using host-language truthiness skips FALSE and numeric zero. |
| `heldout-count-coalesce-and-null-literal` | extension | Public aggregate arguments are bare columns; COALESCE is only used in projection. | Counting source-column non-nullability rather than the evaluated argument returns three filled values instead of four. |
| `heldout-coalesce-inside-versus-outside-sum` | extension | Public aggregate arithmetic does not distinguish scalar evaluation inside an aggregate from evaluation after aggregation. | Hoisting COALESCE across SUM silently changes all-NULL and mixed groups. |
| `heldout-having-null-group-key` | extension | Public HAVING filters by SUM and COUNT; no nullable grouping key is tested in HAVING. | Discarding a NULL group key before HAVING or treating IS NULL as UNKNOWN loses the selected group. |
| `heldout-distinct-after-group-projection` | extension | Public DISTINCT acts on source-row projection, never on outputs of separate aggregate groups. | Applying DISTINCT before grouping or including hidden grouping keys in projected identity retains duplicate outputs. |
| `heldout-later-query-error-aborts-result` | extension | Every public domain-error request has one query; no error follows a successful result. | Streaming partial result arrays before finishing the request violates the atomic error envelope. |
| `heldout-output-alias-shadows-source-sort` | extension | Public output-alias sorting uses names that are not conflicting source bindings. | Binding ORDER BY to source id instead of projected id sorts a different expression. |
| `heldout-unreachable-coalesce-name-check` | extension | Public static unreachable checks exercise types, not names in a scalar fallback function. | Constant folding COALESCE before binding hides an unknown-column error. |
| `heldout-on-cannot-reference-future-join` | extension | Public joins reference only already-visible tables in ON. | Binding all FROM tables globally accepts a forward reference and makes join evaluation ill-defined. |
| `heldout-nested-aggregate-through-coalesce` | extension | Public invalid aggregates cover ungrouped projection and aggregate in ON, not nesting under a scalar function. | Checking only direct aggregate children misses a nested SUM beneath COALESCE. |
| `heldout-ungrouped-reference-in-fallback` | extension | Public ungrouped reference is a direct SELECT item, not nested in a scalar expression beside an aggregate. | Marking the whole expression aggregated when any child is SUM accepts an ungrouped fallback column. |
| `heldout-nullable-boolean-stable-sort` | extension | Public boolean sorting has no NULL; public NULL sorting uses integers. | Reusing numeric NULL sentinels or collapsing false and NULL produces incorrect bool ordering. |
| `heldout-coalesce-preserves-empty-and-zero` | extension | Public COALESCE includes false but lacks stored integer zero and empty text in the same typed evaluation path. | Host-language fallback operators replace zero, false, or empty strings even though all are non-null. |
| `heldout-distinct-outer-join-bag-multiplicity` | extension | Public join multiplicity and DISTINCT NULL are separate; neither combines duplicates on both join inputs with projected padding. | Distinguishing synthetic NULL from stored NULL or deduplicating joins by source identity leaks extra rows. |

Expected successful result values were independently checked using Python’s
SQLite library, with automatic indexes disabled and the specified NULL sorting
defaults made explicit. Where SQLite does not promise this contract’s encounter
order, aggregate-group values were compared without order and encounter order
was reviewed separately. Boolean JSON types and domain-error cases were checked
against the published contract; SQLite is not an oracle for this subset’s static
typing or binding-error codes. No reference implementation is included.
