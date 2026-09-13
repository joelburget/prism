/**
 * White-box checks that the optimizer rewrites, stays semantically neutral, and
 * that incrementally maintained views agree with a fresh query over the same rows.
 */
import { validate } from "./model.ts";
import type { Expr, Plan, Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { Store } from "./store.ts";
const database = [
  {
    name: "t",
    columns: [
      { name: "id", type: "int", nullable: false },
      { name: "v", type: "int", nullable: true },
      { name: "flag", type: "bool", nullable: false },
      { name: "maybe", type: "bool", nullable: true },
    ],
    rows: [
      [1, 10, true, true],
      [2, null, false, null],
      [3, 10, true, false],
      [4, null, false, null],
    ],
  },
  {
    name: "r",
    columns: [
      { name: "id", type: "int", nullable: true },
      { name: "w", type: "int", nullable: true },
    ],
    rows: [
      [1, 10],
      [1, null],
      [3, 20],
      [null, 99],
    ],
  },
];
const { database: tables } = validate({
  protocol_version: 1,
  task: "query-null",
  input: { database, queries: [] },
});
let failures = 0;
function ok(condition: boolean, label: string): void {
  if (!condition) failures++;
  console.log(`${condition ? "ok  " : "FAIL"} ${label}`);
}
function plan(sql: string, rewrite: boolean): Plan {
  const p = bind(new Parser(sql).parse(), tables);
  return rewrite ? optimize(p) : p;
}
function literal(e: Expr): Value | undefined {
  return e.op === "lit" ? (e.value as Value) : undefined;
}
function projection(sql: string): Expr {
  return plan(sql, true).query.select[0][0];
}
/* Constant folding still collapses pure literal arithmetic and booleans. */
ok(literal(projection("SELECT 1 + 2 * 3 AS x FROM t")) === 7, "fold arithmetic");
ok(
  literal(projection("SELECT FALSE AND NULL AS x FROM t")) === false,
  "fold FALSE AND UNKNOWN to FALSE",
);
ok(
  literal(projection("SELECT TRUE AND NULL AS x FROM t")) === null,
  "fold TRUE AND UNKNOWN to UNKNOWN",
);
ok(
  literal(projection("SELECT COALESCE(NULL, NULL) AS x FROM t")) === null,
  "fold all-NULL COALESCE",
);
ok(
  literal(projection("SELECT NULL IS NULL AS x FROM t")) === true,
  "fold IS NULL over a literal",
);
/* Tautologies are folded only where nullability rules them out. */
ok(literal(projection("SELECT id = id AS x FROM t")) === true, "fold x = x when non-nullable");
ok(
  projection("SELECT v = v AS x FROM t").op === "=",
  "keep x = x when nullable",
);
ok(
  literal(projection("SELECT flag OR NOT flag AS x FROM t")) === true,
  "fold excluded middle when non-nullable",
);
ok(
  projection("SELECT maybe OR NOT maybe AS x FROM t").op === "OR",
  "keep excluded middle when nullable",
);
ok(
  projection("SELECT COUNT(*) = COUNT(*) AS x FROM t").op === "=",
  "never fold across aggregates",
);
/* Single-source filter pushdown, restricted around outer joins. */
const inner = plan(
  "SELECT t.id AS a FROM t JOIN r ON t.id = r.id WHERE r.w > 5 AND t.id + r.w > 2",
  true,
);
ok(inner.filters[1].length === 1, "push single-source conjunct into inner scan");
ok(inner.query.where !== undefined, "keep cross-source conjunct above the join");
const outer = plan(
  "SELECT t.id AS a FROM t LEFT JOIN r ON t.id = r.id WHERE r.w IS NULL",
  true,
);
ok(
  outer.filters[1].length === 0 && outer.query.joins[0].outer,
  "never push a null-accepting filter below an outer join",
);
const rejecting = plan(
  "SELECT t.id AS a FROM t LEFT JOIN r ON t.id = r.id WHERE r.w > 15",
  true,
);
ok(
  !rejecting.query.joins[0].outer && rejecting.filters[1].length === 1,
  "null-rejecting filter turns the outer join inner and then pushes down",
);
const leftSide = plan(
  "SELECT t.id AS a FROM t LEFT JOIN r ON t.id = r.id WHERE t.id > 2",
  true,
);
ok(
  leftSide.filters[0].length === 1 && leftSide.query.joins[0].outer,
  "push a left-side filter below an outer join without changing it",
);
/* Differential check: optimized execution must match unoptimized execution. */
const queries = [
  "SELECT id AS a, v AS b FROM t WHERE v = v ORDER BY b NULLS FIRST, a DESC",
  "SELECT t.id AS a, r.w AS b FROM t LEFT JOIN r ON t.id = r.id WHERE r.w > 5",
  "SELECT t.id AS a, r.w AS b FROM t LEFT JOIN r ON t.id = r.id WHERE r.w IS NULL",
  "SELECT t.id AS a FROM t LEFT JOIN r ON t.id = r.id AND t.id > 1 ORDER BY a",
  "SELECT v AS k, COUNT(*) AS n, SUM(v) AS s, MIN(v) AS lo FROM t GROUP BY v",
  "SELECT SUM(v) AS s, MAX(v) AS hi, COUNT(v) AS c FROM t WHERE v IS NULL",
  "SELECT DISTINCT COALESCE(v, 0) AS x FROM t ORDER BY x DESC LIMIT 2 OFFSET 1",
  "SELECT maybe OR NOT maybe AS x, flag OR NOT flag AS y FROM t",
  "SELECT t.id AS a, q.w AS b FROM t LEFT OUTER JOIN r AS q ON t.id = q.id JOIN r ON r.w = 99",
];
for (const sql of queries) {
  const a = JSON.stringify(execute(plan(sql, false)));
  const b = JSON.stringify(execute(plan(sql, true)));
  ok(a === b, `optimizer preserves results: ${sql}`);
}
/* Maintained views must equal a one-shot query over the live rows after every batch. */
function snapshot(store: Store): Map<string, Table> {
  const data = [...store.bases.entries()].map(([name, b]) => ({
    name,
    columns: b.table.columns,
    rows: [...b.rows.values()]
      .sort((x, y) => x.seq - y.seq)
      .map((r) => r.values),
  }));
  return validate({
    protocol_version: 1,
    task: "query-null",
    input: { database: data, queries: [] },
  }).database;
}
function maintains(sql: string, rewrite: boolean, batches: unknown[][]): void {
  const store = new Store(tables);
  store.run([{ op: "create", view: "v", sql, optimize: rewrite }]);
  batches.forEach((changes, step) => {
    const applied = store.run([{ op: "apply", changes }])[0] as {
      ok: boolean;
    };
    ok(applied.ok, `batch ${step} applies: ${sql}`);
    const got = (store.run([{ op: "read", view: "v" }])[0] as {
      result: { rows: Value[][] };
    }).result.rows;
    const want = execute(
      bind(new Parser(sql).parse(), snapshot(store)),
    ).rows;
    ok(
      JSON.stringify(got) === JSON.stringify(want),
      `maintained after batch ${step}: ${sql}`,
    );
  });
}
const script: unknown[][] = [
  [{ op: "delete", table: "r", id: 1 }],
  [{ op: "insert", table: "r", id: 5, row: [1, 7] }],
  [
    { op: "update", table: "t", id: 2, row: [2, 10, true, null] },
    { op: "update", table: "r", id: 3, row: [2, 20] },
  ],
  [{ op: "delete", table: "r", id: 5 }, { op: "delete", table: "r", id: 3 }],
  [{ op: "insert", table: "t", id: 9, row: [9, null, false, true] }],
  [{ op: "delete", table: "t", id: 1 }],
];
for (const sql of queries)
  for (const rewrite of [false, true]) maintains(sql, rewrite, script);
/* Bag multiplicity, group lifetime and batch atomicity. */
const store = new Store(tables);
store.run([
  {
    op: "create",
    view: "v",
    sql: "SELECT DISTINCT v AS x FROM t ORDER BY x NULLS LAST",
    optimize: true,
  },
]);
const read = (): unknown =>
  (store.run([{ op: "read", view: "v" }])[0] as { result: unknown }).result;
ok(
  JSON.stringify(read()) ===
    '{"revision":0,"columns":["x"],"rows":[[10],[null]]}',
  "duplicates collapse into one DISTINCT row",
);
store.run([{ op: "apply", changes: [{ op: "delete", table: "t", id: 1 }] }]);
ok(
  JSON.stringify(read()) ===
    '{"revision":1,"columns":["x"],"rows":[[10],[null]]}',
  "one of two duplicate contributions leaves the row in place",
);
const rejected = store.run([
  {
    op: "apply",
    changes: [
      { op: "delete", table: "t", id: 3 },
      { op: "delete", table: "t", id: 3 },
    ],
  },
]);
ok(
  JSON.stringify(rejected) === '[{"ok":false,"error":{"code":"UNKNOWN_ROW"}}]',
  "a failing change rejects its whole batch",
);
ok(
  JSON.stringify(read()) ===
    '{"revision":1,"columns":["x"],"rows":[[10],[null]]}',
  "a rejected batch leaves neither rows nor revision behind",
);
store.run([{ op: "apply", changes: [{ op: "delete", table: "t", id: 3 }] }]);
ok(
  JSON.stringify(read()) ===
    '{"revision":2,"columns":["x"],"rows":[[null]]}',
  "the last contribution removes the DISTINCT row",
);
ok(
  JSON.stringify(store.run([{ op: "apply", changes: [] }])) ===
    '[{"ok":false,"error":{"code":"INVALID_COMMAND"}}]',
  "an empty batch is rejected",
);
console.log(failures ? `${failures} failing check(s)` : "all checks passed");
process.exit(failures ? 1 : 0);
