# Query engine: NULL and left outer joins

## Change request

Extend an existing, in-memory relational query engine to support SQL NULL,
three-valued logic, and left outer joins. Preserve its non-null query behavior
and keep optimized execution semantically equivalent to unoptimized execution.
The change touches input validation, parsing, binding/type checking, expression
evaluation, joining, aggregation, sorting, and optimizer rules.

Each evaluation run will receive one starter in its assigned language. Prism and
the comparison language run in separate, fresh agent sessions with no access to
each other's code or run artifacts, as described in the parent README.
This directory specifies
their baseline contract and the target extension; it contains no starter or
reference implementation. The task is to change the supplied engine, retaining
its architecture where practical. Do not delegate query evaluation to another SQL
engine. Standard libraries for JSON, collections, and sorting are allowed.

## Baseline and requested extension

The baseline supports non-null typed tables, SELECT expressions, WHERE, INNER
JOIN, GROUP BY, HAVING, DISTINCT, ORDER BY, LIMIT/OFFSET, COUNT, and grouped
SUM/MIN/MAX. It has a parser, bound logical plan, execution engine, and optional
optimizer. The eventual starter must implement actual safe transformations such
as constant folding and pushing single-table filters through inner joins.

The extension adds nullable columns and NULL literals, three-valued expressions,
IS NULL / IS NOT NULL, COALESCE, LEFT [OUTER] JOIN, explicit NULLS FIRST/LAST,
and global SUM/MIN/MAX (including empty input). Adapt optimizer transformations
to preserve these semantics. Existing inner-join and constant-folding optimization
capabilities must continue to work; disabling all optimization is not a satisfactory
implementation. Public black-box cases check behavior in both modes but cannot
prove that optimizations execute; reviewers must inspect the implementation.

## Wire protocol

Read one JSON object from stdin and print one JSON object plus a newline to
stdout. Logs go to stderr. Each request runs in a fresh process; successful
requests and domain errors both exit with status zero.

```json
{"protocol_version":1,"task":"query-null","input":{"database":[{"name":"items","columns":[{"name":"id","type":"int","nullable":false},{"name":"price","type":"int","nullable":true}],"rows":[[1,10],[2,null]]}],"queries":[{"sql":"SELECT id AS id, price IS NULL AS missing FROM items ORDER BY id","optimize":true}]}}
```

Response:

```json
{"ok":true,"result":{"results":[{"columns":["id","missing"],"rows":[[1,false],[2,true]]}]}}
```

Each query sees the same immutable database. The `results` array follows query
order. On a domain error return only `{"ok":false,"error":{"code":"CODE"}}`;
do not return partial query results or variable diagnostic text. Validate the
entire database first, then parse, bind, and execute queries in array order.
The first failing query determines the request error. Each input has only one
intended error class, so precedence between independent errors need not be defined.

All fields shown are required. Within `input`, table objects, column objects, and
query objects, unknown fields are rejected as `INVALID_INPUT`. `database`,
`columns`, `rows`, and `queries` are arrays; each row is an array; `sql` is a
string; `optimize` and `nullable` are JSON booleans, never integers or strings.
Table and column names, aliases, and identifiers
are lowercase ASCII matching `[a-z_][a-z0-9_]*`; keywords are case-insensitive.
Names are case-sensitive and must be unique within their respective namespace.
The words appearing as keywords or function names in the SQL grammar below are
reserved and cannot be schema names or aliases (including `first`, `last`,
`null`, and `count`). Schema violations are `INVALID_INPUT`; reserved words
used as SQL identifiers are `PARSE_ERROR`. Quoted identifiers are unsupported.
Types are `int`, `text`, and `bool`; JSON booleans are not integers. Rows are
arrays in column declaration order. NULL is JSON `null`, allowed in table data
only when the column declares `nullable:true`. Tables may be empty but must have
at least one column. An empty database and empty query list are allowed.
Fixtures use ASCII text and integer inputs/intermediates within ±1,000,000,000;
behavior outside these bounds is out of scope.

## SQL subset

This is a deliberately specified subset, not unrestricted SQL compatibility.
Every SELECT item must have an explicit unique `AS alias`. There is no SELECT
`*` expansion; `*` is supported only in COUNT. Table aliases use `AS` and hide
the original table name. A FROM clause is mandatory. A final semicolon is optional.

```text
SELECT [DISTINCT] expression AS alias [, ...]
FROM table [AS alias]
{ [INNER] JOIN table [AS alias] ON expression
| LEFT [OUTER] JOIN table [AS alias] ON expression }*
[WHERE expression]
[GROUP BY column_reference [, ...]]
[HAVING expression]
[ORDER BY output_alias [ASC|DESC] [NULLS FIRST|NULLS LAST] [, ...]]
[LIMIT nonnegative_integer [OFFSET nonnegative_integer]]
```

Expressions comprise column references (`column` or `alias.column`), integer
literals including negative integers, single-quoted text (escape a quote with
`''`), TRUE/FALSE/NULL, parentheses, `+`, `-`, `*`, `=`, `<>`, `<`, `<=`, `>`,
`>=`, AND, OR, NOT, IS NULL, IS NOT NULL, COALESCE with at least two arguments,
and aggregates COUNT(*), COUNT(expr), SUM(expr), MIN(expr), MAX(expr).
There are no comments, division, IN, CASE, subqueries, implicit casts, or other
functions. Binary precedence, high to low: multiplication; addition/subtraction;
comparisons and IS [NOT] NULL; NOT; AND; OR. Arithmetic operators of equal
precedence associate left. Chained comparisons are invalid. Unary NOT is
supported; unary minus is required only as part of an integer literal.

Unqualified column references must resolve uniquely among all source tables.
JOIN ON can reference only tables available at that join. SELECT aliases are
visible only in ORDER BY. WHERE, ON, HAVING, AND, OR, and NOT require booleans
(an untyped NULL is allowed in boolean context). Arithmetic and SUM require int;
comparisons require compatible operand types. Booleans support equality and
inequality only. MIN/MAX support int/text. COALESCE arguments must have a common
type, ignoring untyped NULL literals; all-NULL arguments are allowed. NULL can
be assigned the type required by its context. Check types and names before
execution, including on empty tables and in unreachable expressions.

Aggregates are allowed only in SELECT/HAVING and cannot be nested. GROUP BY
allows column references only; duplicate group keys are invalid. In an aggregate
query, each column reference outside an aggregate must be a GROUP BY key.
Expressions over group keys and aggregates are allowed. HAVING requires GROUP BY
or an aggregate in SELECT/HAVING. GROUP BY without aggregates is allowed. Without
GROUP BY, aggregates produce exactly one group even for empty input. Without
aggregates or GROUP BY, an empty input produces no rows. The baseline only needs
COUNT for global aggregation; global SUM/MIN/MAX belong to the extension.

## Defined evaluation behavior

1. Read table rows in input order. Join left to right. For each left row, visit
   right rows in input order and emit each pair whose ON condition is TRUE.
   INNER JOIN drops a row with no match. LEFT JOIN instead emits one row padded
   with NULL for every right-side column, regardless of declared nullability.
   ON FALSE and ON UNKNOWN both fail to match. A NULL key never equals a NULL key.
2. WHERE and HAVING retain only TRUE. FALSE and UNKNOWN are both discarded.
   Arithmetic or ordinary comparison with a NULL operand returns NULL. NOT NULL
   returns NULL. FALSE AND NULL is FALSE; TRUE AND NULL is NULL; TRUE OR NULL is
   TRUE; FALSE OR NULL is NULL; NULL AND NULL and NULL OR NULL are NULL.
   IS NULL/IS NOT NULL always return a non-null boolean. COALESCE returns its
   first non-null argument, or NULL if none exists.
3. Group keys use equality with all NULL values in a key position treated as
   the same key. Groups appear in order of first encounter. COUNT(*) counts all
   rows; COUNT(expr) counts non-null values. SUM/MIN/MAX ignore NULL values and
   return NULL if no non-null values remain. COUNT returns zero in an empty
   global group. SUM is exact integer addition. Text comparison uses ASCII
   lexicographic order. There is no collation or locale setting.
4. Evaluate SELECT, then DISTINCT, then ORDER BY, then OFFSET/LIMIT. DISTINCT
   compares full projected rows, treating NULLs as equal, and retains the first
   occurrence. ORDER BY refers only to output aliases and is stable on ties.
   ASC is the default. NULL placement defaults to LAST for ASC and FIRST for
   DESC; an explicit NULLS clause overrides direction. Booleans sort false
   before true. Without ORDER BY, preserve the encounter order defined above.
   OFFSET defaults to zero, requires LIMIT, and is applied before LIMIT.

Optimizations must preserve this contract, including stable output order. A
filter on right-side columns cannot in general be moved from WHERE to the ON
clause of a left join. `x = x` is not always TRUE, and `p OR NOT p` is not always
TRUE. Folding these expressions must account for possible NULL values introduced
by outer joins as well as declared nullable columns.

## Errors

| Code | Meaning |
| --- | --- |
| `INVALID_INPUT` | Malformed input structure, invalid/duplicate schema names, wrong row width/type, or NULL in a non-nullable input column. |
| `PARSE_ERROR` | SQL outside the grammar, unknown function, missing mandatory alias, or malformed literal. |
| `UNKNOWN_TABLE` | Table does not exist. |
| `UNKNOWN_COLUMN` | Column, qualifier, or ORDER BY output alias does not exist. |
| `AMBIGUOUS_COLUMN` | An unqualified column matches multiple source columns. |
| `TYPE_ERROR` | Expression operands or predicate have incompatible types. |
| `INVALID_AGGREGATION` | Nested/misplaced aggregate, illegal ungrouped reference, invalid HAVING, or duplicate group key. |
| `DUPLICATE_ALIAS` | Duplicate SELECT output alias or duplicate source table qualifier. |

## Acceptance and review

`cases.json` contains public, fixed input/output examples, separated into baseline
regressions and extension behavior. Every successful nonempty query scenario is
run in both optimizer modes. Expected outputs are explicit JSON values, not
generated by the implementation under evaluation. They cover three-valued truth
tables, nullable arithmetic/comparisons, grouping, empty aggregation, left-join
multiplicity, ON versus WHERE, null-rejecting filters, nested joins, and diagnostics.

Use the common runner one directory above; its timeout defaults to 10 seconds
and is configurable. These are behavioral tests, not performance benchmarks.
Report baseline and extension results separately. Passing this public corpus is
evidence of conformance, not a complete correctness proof or hidden-test score.
Future evaluation should add unseen databases and queries and review optimizer
legality, propagation of nullability, regression scope, and clarity of the
representation of TRUE/FALSE/UNKNOWN. No reference implementation is required
to execute this fixed-output suite.
