/** Run with `node --test`. Differential fuzz: maintained views must equal fresh queries after every batch. */
import { test } from "node:test";
import assert from "node:assert/strict";
import type { Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { Database } from "./views.ts";
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}
const pick = <T>(r: () => number, xs: T[]): T => xs[Math.floor(r() * xs.length)];
const schema: Table[] = [
  { name: "a", columns: [{ name: "k", type: "int", nullable: true }, { name: "v", type: "int", nullable: true }, { name: "s", type: "text", nullable: true }], rows: [] },
  { name: "b", columns: [{ name: "k", type: "int", nullable: true }, { name: "w", type: "int", nullable: true }], rows: [] },
  { name: "c", columns: [{ name: "k", type: "int", nullable: false }, { name: "f", type: "bool", nullable: true }], rows: [] },
];
function randomRow(r: () => number, t: Table): Value[] {
  return t.columns.map((c) => {
    if (c.nullable && r() < 0.2) return null;
    if (c.type === "int") return Math.floor(r() * 5);
    if (c.type === "bool") return r() < 0.5;
    return pick(r, ["x", "y", "zz", "a"]);
  });
}
function randomSql(r: () => number): string {
  const n = 1 + Math.floor(r() * 3);
  const names = Array.from({ length: n }, () => pick(r, ["a", "b", "c"]));
  const col = (i: number): string => {
    const t = names[i];
    return `t${i}.${t === "a" ? pick(r, ["k", "v"]) : t === "b" ? pick(r, ["k", "w"]) : "k"}`;
  };
  let sql = `FROM ${names[0]} AS t0`;
  for (let i = 1; i < n; i++) {
    const kind = r() < 0.6 ? "LEFT JOIN" : "JOIN";
    const j = Math.floor(r() * i);
    const on = pick(r, [
      `${col(j)} = ${col(i)}`,
      `${col(j)} = ${col(i)} AND ${col(i)} > 1`,
      `${col(j)} < ${col(i)}`,
      `${col(j)} = ${col(i)} OR ${col(j)} = 2`,
      `t${j}.k = t${i}.k`,
    ]);
    sql += ` ${kind} ${names[i]} AS t${i} ON ${on}`;
  }
  if (r() < 0.6) {
    const i = Math.floor(r() * n);
    sql += ` WHERE ${pick(r, [`${col(i)} IS NULL`, `${col(i)} > 1`, `COALESCE(${col(i)}, 0) = 0`, `${col(i)} + 1 <> ${col(0)}`, `${col(i)} IS NOT NULL AND ${col(0)} < 4`, `NOT ${col(i)} = 2`])}`;
  }
  const aggregate = r() < 0.5;
  const items: string[] = [];
  let group = "";
  if (aggregate) {
    const keys = r() < 0.3 ? [] : [`t0.k`, ...(n > 1 && r() < 0.5 ? [col(1)] : [])];
    group = keys.length ? ` GROUP BY ${keys.join(", ")}` : "";
    keys.forEach((k, i) => items.push(`${k} AS g${i}`));
    items.push(`COUNT(*) AS n`, `COUNT(${col(n - 1)}) AS nc`, `SUM(${col(n - 1)}) AS s`, `MIN(${col(0)}) AS lo`, `MAX(${col(n - 1)}) AS hi`);
    if (names[0] === "a") items.push(`MAX(t0.s) AS hs`);
    if (r() < 0.5) items.push(`COUNT(*) - COUNT(${col(0)}) AS d`);
    if (r() < 0.4) group += ` HAVING ${pick(r, ["COUNT(*) > 1", `SUM(${col(n - 1)}) IS NULL OR SUM(${col(n - 1)}) > 3`, `MIN(${col(0)}) = 1`])}`;
  } else {
    for (let i = 0; i < n; i++) items.push(`${col(i)} AS c${i}`);
    if (r() < 0.5) items.push(`COALESCE(${col(n - 1)}, -1) * 2 AS e`);
    if (names[0] === "a" && r() < 0.5) items.push(`t0.s AS s`);
  }
  const aliases = items.map((i) => i.split(" AS ")[1]);
  sql = `SELECT ${r() < 0.3 ? "DISTINCT " : ""}${items.join(", ")} ${sql}${group}`;
  if (r() < 0.7) {
    const order = [...aliases].sort(() => r() - 0.5).slice(0, 1 + Math.floor(r() * 2));
    sql += ` ORDER BY ${order.map((a) => `${a}${pick(r, ["", " ASC", " DESC"])}${pick(r, ["", " NULLS FIRST", " NULLS LAST"])}`).join(", ")}`;
  }
  if (r() < 0.4) sql += ` LIMIT ${Math.floor(r() * 5)}${r() < 0.5 ? ` OFFSET ${Math.floor(r() * 3)}` : ""}`;
  return sql;
}
function fresh(db: Database, sql: string, opt: boolean): { columns: string[]; rows: Value[][] } {
  const tables = new Map<string, Table>();
  for (const [name, t] of db.tables)
    tables.set(name, { name, columns: t.columns, rows: [...t.rows.values()].sort((x, y) => x.pos - y.pos).map((s) => s.row) });
  const plan = bind(new Parser(sql).parse(), tables);
  return execute(opt ? optimize(plan) : plan);
}
test("maintained views agree with fresh evaluation across random batches", () => {
  for (let seed = 1; seed <= 150; seed++) {
    const r = rng(seed);
    const initial = schema.map((t) => ({ ...t, rows: Array.from({ length: Math.floor(r() * 6) }, () => randomRow(r, t)) }));
    const db = new Database(new Map(initial.map((t) => [t.name, t])));
    const views: [string, string, boolean][] = [];
    for (let i = 0; i < 4; i++) {
      const sql = randomSql(r);
      const opt = r() < 0.5;
      const reply = db.run([{ op: "create", view: `v${i}`, sql, optimize: opt }])[0];
      assert.ok(reply.ok, `${sql}: ${JSON.stringify(reply)}`);
      views.push([`v${i}`, sql, opt]);
    }
    const ids = new Map(initial.map((t) => [t.name, t.rows.length]));
    for (let step = 0; step < 12; step++) {
      const changes = Array.from({ length: 1 + Math.floor(r() * 4) }, () => {
        const t = pick(r, initial);
        const op = pick(r, ["insert", "update", "update", "delete"]);
        const live = [...db.tables.get(t.name)!.rows.keys()];
        if (op === "insert") {
          const id = r() < 0.1 ? 1 + Math.floor(r() * 3) : ids.set(t.name, ids.get(t.name)! + 1).get(t.name)!;
          return { op, table: t.name, id, row: randomRow(r, t) };
        }
        const id = live.length && r() < 0.9 ? pick(r, live) : 99;
        return op === "delete" ? { op, table: t.name, id } : { op, table: t.name, id, row: randomRow(r, t) };
      });
      db.run([{ op: "apply", changes }]);
      for (const [name, sql, opt] of views) {
        const reply = db.run([{ op: "read", view: name }])[0];
        assert.ok(reply.ok);
        const { columns, rows } = reply.result as { columns: string[]; rows: Value[][] };
        assert.deepEqual({ columns, rows }, fresh(db, sql, opt), `seed ${seed} step ${step}: ${sql}\n${JSON.stringify(changes)}`);
      }
    }
  }
});
test("failed batches leave views and revision untouched", () => {
  const db = new Database(new Map([["c", { ...schema[2], rows: [[1, true], [2, null]] }]]));
  db.run([{ op: "create", view: "v", sql: "SELECT k AS k, COUNT(*) AS n FROM c GROUP BY k", optimize: true }]);
  const before = db.run([{ op: "read", view: "v" }])[0];
  const failed = db.run([{ op: "apply", changes: [{ op: "insert", table: "c", id: 3, row: [3, false] }, { op: "update", table: "c", id: 9, row: [1, null] }] }])[0];
  assert.deepEqual(failed, { ok: false, error: { code: "UNKNOWN_ROW" } });
  assert.deepEqual(db.run([{ op: "read", view: "v" }])[0], before);
  assert.equal(db.run([{ op: "apply", changes: [{ op: "insert", table: "c", id: 3, row: [3, false] }] }])[0].ok, true);
  assert.deepEqual(db.run([{ op: "read", view: "v" }])[0], { ok: true, result: { revision: 1, columns: ["k", "n"], rows: [[1, 1], [2, 1], [3, 1]] } });
});
test("reads return fresh copies", () => {
  const db = new Database(new Map([["c", { ...schema[2], rows: [[1, true]] }]]));
  db.run([{ op: "create", view: "v", sql: "SELECT k AS k FROM c", optimize: false }]);
  const first = db.run([{ op: "read", view: "v" }])[0] as { result: { rows: Value[][] } };
  first.result.rows[0][0] = 99;
  assert.deepEqual((db.run([{ op: "read", view: "v" }])[0] as { result: { rows: Value[][] } }).result.rows, [[1]]);
});
