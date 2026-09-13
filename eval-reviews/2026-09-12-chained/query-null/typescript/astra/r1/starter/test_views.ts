/** Differential regression: retained state versus checkpoint-one execution.
 * Run: node starter/test_views.ts */
import assert from "node:assert/strict";
import { commands } from "./views.ts";
import { validate } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { execute, optimize } from "./engine.ts";
import type { Value } from "./model.ts";

const sqls = [
  "SELECT a.k AS k, a.v AS v FROM a",
  "SELECT DISTINCT k AS k FROM a ORDER BY k DESC NULLS LAST LIMIT 3 OFFSET 1",
  "SELECT k AS k, COUNT(*) AS n, COUNT(v) AS c, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM a GROUP BY k",
  "SELECT COUNT(*) AS n, SUM(v) AS s, MIN(v) AS lo, MAX(v) AS hi FROM a WHERE v > 0",
  "SELECT k AS k, COALESCE(SUM(v), 0) + COUNT(*) AS n FROM a GROUP BY k HAVING SUM(v) IS NULL OR COUNT(*) > 1 ORDER BY n DESC LIMIT 3",
  "SELECT a.k AS x, b.v AS y FROM a LEFT JOIN b ON a.k = b.k",
  "SELECT a.k AS x, b.v AS y FROM a LEFT JOIN b ON a.k = b.k AND b.v > a.v WHERE b.v IS NULL OR a.v > 0",
  "SELECT a.k AS x, b.v AS y FROM a JOIN b ON a.k = b.k WHERE a.v > 0 AND b.v < 3",
  "SELECT a.k AS x, b.v AS y, c.v AS z FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.v = c.k",
  "SELECT a.k AS x, b.v AS y, c.v AS z FROM a LEFT JOIN b ON a.k = b.k JOIN c ON a.k = c.k WHERE c.v > 0",
  "SELECT a.k AS x, COUNT(*) AS n, COUNT(c.v) AS c, SUM(b.v) AS s, MIN(c.v) AS lo FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.v = c.k GROUP BY a.k",
  "SELECT l.k AS k, r.v AS v FROM a AS l LEFT JOIN a AS r ON l.k = r.v",
  "SELECT a.k AS x, b.v AS y FROM a LEFT JOIN b ON a.k < b.k AND a.v <> b.v",
  "SELECT a.k AS x, b.v AS y FROM a LEFT JOIN b ON COALESCE(a.k, 1) + a.v = COALESCE(b.k, 2) + b.v",
  "SELECT DISTINCT a.v AS x, c.v AS y FROM a LEFT JOIN b ON a.k = b.k LEFT JOIN c ON b.v = c.k ORDER BY x ASC NULLS FIRST, y DESC LIMIT 5 OFFSET 2",
  "SELECT a.k AS x, COUNT(*) AS n FROM a LEFT JOIN b ON TRUE GROUP BY a.k HAVING COUNT(*) > 2",
];
const envelope = (input: unknown) => ({ protocol_version: 1, task: "query-null", input });
let comparisons = 0;
for (let seed = 1; seed <= 12; seed++) {
  let state = seed;
  const random = (n: number): number => { state = (Math.imul(state, 1664525) + 1013904223) >>> 0; return (state >>> 8) % n; };
  const row = (): Value[] => [random(4) === 0 ? null : random(5) - 2, random(4) === 0 ? null : random(7) - 3];
  const names = ["a", "b", "c"];
  const live = names.map(() => new Map<number, Value[]>(Array.from({ length: 5 }, (_, i) => [i + 1, row()])));
  const next = [6, 6, 6];
  const database = () => names.map((name, i) => ({ name, columns: ["k", "v"].map(name => ({ name, type: "int", nullable: true })), rows: [...live[i].values()] }));
  const initial = database();
  const cmds: unknown[] = [];
  const expected: unknown[] = [];
  const views = sqls.flatMap(sql => [false, true].map(optimize => ({ sql, optimize })));
  views.forEach((q, i) => { cmds.push({ op: "create", view: `v${i}`, ...q }); expected.push({ ok: true, result: { view: `v${i}`, revision: 0 } }); });
  for (let revision = 0; revision <= 40; revision++) {
    if (revision) {
      const changes = [];
      for (let j = 0, n = random(5) + 1; j < n; j++) {
        const t = random(3);
        const ids = [...live[t].keys()];
        const op = ids.length ? random(3) : 0;
        const id = op ? ids[random(ids.length)] : next[t]++;
        const value = row();
        changes.push(op === 2 ? { op: "delete", table: names[t], id } : { op: op ? "update" : "insert", table: names[t], id, row: value });
        if (op === 2) live[t].delete(id); else live[t].set(id, value);
      }
      cmds.push({ op: "apply", changes });
      expected.push({ ok: true, result: { revision } });
    }
    const [db] = validate(envelope({ database: database(), queries: [] }));
    views.forEach((q, i) => {
      cmds.push({ op: "read", view: `v${i}` });
      const plan = bind(new Parser(q.sql).parse(), db);
      expected.push({ ok: true, result: { revision, ...execute(q.optimize ? optimize(plan) : plan) } });
      comparisons++;
    });
  }
  const actual = commands(envelope({ database: initial, commands: cmds }));
  actual.forEach((r, i) => assert.deepEqual(r, expected[i], `seed ${seed}, command ${i}: ${JSON.stringify(cmds[i])}`));
}

// Shape prevalidation beats row/table/ID validation; failed insertions reserve
// neither IDs nor encounter positions. Earlier reads remain immutable.
const database = [{ name: "t", columns: [{ name: "x", type: "int", nullable: false }], rows: [[1]] }];
const lifecycle = commands(envelope({ database, commands: [
  { op: "create", view: "v", sql: "SELECT x AS x FROM t", optimize: true },
  { op: "read", view: "v" },
  { op: "apply", changes: [{ op: "insert", table: "absent", id: 2, row: [2] }, { op: "delete", table: "t", id: false }] },
  { op: "apply", changes: [{ op: "insert", table: "t", id: 2, row: [2] }, { op: "update", table: "t", id: 1, row: [1000000001] }] },
  { op: "apply", changes: [{ op: "insert", table: "t", id: 2, row: [2] }, { op: "delete", table: "t", id: 2 }] },
  { op: "apply", changes: [{ op: "insert", table: "t", id: 2, row: [2] }] },
  { op: "apply", changes: [{ op: "update", table: "t", id: 1, row: [3] }] },
  { op: "read", view: "v" },
  { op: "drop", view: "v" },
  { op: "create", view: "v", sql: "SELECT x AS x FROM t", optimize: false },
  { op: "read", view: "v" },
] }));
assert.deepEqual(lifecycle, [
  { ok: true, result: { view: "v", revision: 0 } },
  { ok: true, result: { revision: 0, columns: ["x"], rows: [[1]] } },
  { ok: false, error: { code: "INVALID_COMMAND" } },
  { ok: false, error: { code: "INVALID_ROW" } },
  { ok: true, result: { revision: 1 } },
  { ok: false, error: { code: "ROW_ID_USED" } },
  { ok: true, result: { revision: 2 } },
  { ok: true, result: { revision: 2, columns: ["x"], rows: [[3]] } },
  { ok: true, result: { dropped: "v" } },
  { ok: true, result: { view: "v", revision: 2 } },
  { ok: true, result: { revision: 2, columns: ["x"], rows: [[3]] } },
]);
console.log(`${comparisons} differential view snapshots and lifecycle regressions passed`);

// Exercise JSON number-token handling at the actual launch entrypoint.
const { spawnSync } = await import("node:child_process");
const wire = spawnSync(process.execPath, [new URL("./main.ts", import.meta.url).pathname], {
  input: JSON.stringify(envelope({ database, commands: [
    { op: "apply", changes: [{ op: "update", table: "t", id: 1.5, row: [4] }] },
    { op: "apply", changes: [{ op: "update", table: "t", id: 1, row: [4.5] }] },
    { op: "create", view: "v", sql: "SELECT x AS x FROM t", optimize: true },
    { op: "read", view: "v" },
  ] })), encoding: "utf8",
});
assert.equal(wire.status, 0, wire.stderr);
assert.deepEqual(JSON.parse(wire.stdout), { ok: true, result: { results: [
  { ok: false, error: { code: "INVALID_COMMAND" } },
  { ok: false, error: { code: "INVALID_ROW" } },
  { ok: true, result: { view: "v", revision: 0 } },
  { ok: true, result: { revision: 0, columns: ["x"], rows: [[1]] } },
] } });
console.log("Command-level numeric wire errors passed");
