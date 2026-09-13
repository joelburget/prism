/** Run with `node --test` from this directory. Checks optimizer legality and mode equivalence. */
import { test } from "node:test";
import assert from "node:assert/strict";
import type { Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
const database = new Map<string, Table>([
  [
    "l",
    {
      name: "l",
      columns: [
        { name: "id", type: "int", nullable: false },
        { name: "k", type: "int", nullable: false },
      ],
      rows: [
        [1, 1],
        [2, 2],
        [3, 3],
        [4, 4],
      ],
    },
  ],
  [
    "r",
    {
      name: "r",
      columns: [
        { name: "id", type: "int", nullable: true },
        { name: "v", type: "int", nullable: true },
      ],
      rows: [
        [1, 10],
        [1, null],
        [2, 20],
        [null, 99],
      ],
    },
  ],
  [
    "s",
    {
      name: "s",
      columns: [
        { name: "id", type: "int", nullable: false },
        { name: "name", type: "text", nullable: false },
      ],
      rows: [
        [10, "ten"],
        [20, "twenty"],
      ],
    },
  ],
]);
const plan = (sql: string) => bind(new Parser(sql).parse(), database);
const run = (sql: string, opt: boolean): Value[][] =>
  execute(opt ? optimize(plan(sql)) : plan(sql)).rows;
test("inner-join single-source conjuncts are pushed into scans", () => {
  const p = optimize(
    plan("SELECT l.id AS id FROM l JOIN r ON l.id = r.id WHERE l.k > 1 AND r.v > 5 AND l.k < r.v"),
  );
  assert.equal(p.filters[0].length, 1);
  assert.equal(p.filters[1].length, 1);
  assert.equal(p.query.where?.op, "<");
});
test("null-rejecting WHERE on the padded side reduces LEFT JOIN to INNER JOIN and is pushed", () => {
  const p = optimize(plan("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v > 15"));
  assert.deepEqual(p.query.outer, [false]);
  assert.equal(p.filters[1].length, 1);
  assert.equal(p.query.where, undefined);
});
test("NULL-accepting WHERE on the padded side stays above the LEFT JOIN", () => {
  const p = optimize(plan("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL"));
  assert.deepEqual(p.query.outer, [true]);
  assert.equal(p.filters[1].length, 0);
  assert.equal(p.query.where?.op, "IS NULL");
});
test("preserved-side WHERE conjuncts are pushed below a LEFT JOIN", () => {
  const p = optimize(plan("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id WHERE l.k > 1"));
  assert.deepEqual(p.query.outer, [true]);
  assert.equal(p.filters[0].length, 1);
  assert.equal(p.query.where, undefined);
});
test("ON conjuncts are never pushed into scans", () => {
  const p = optimize(plan("SELECT l.id AS id FROM l LEFT JOIN r ON l.id = r.id AND l.id > 1"));
  assert.deepEqual(p.filters, [[], []]);
  assert.equal(p.query.joins[0].op, "AND");
});
test("constant folding respects three-valued logic", () => {
  const p = optimize(
    plan(
      "SELECT NULL = NULL AS a, NOT NULL AS b, FALSE AND NULL AS c, TRUE OR NULL AS d, COALESCE(NULL, NULL) AS e, 1 + 2 * 3 AS f, NULL IS NULL AS g FROM l",
    ),
  );
  assert.deepEqual(
    p.query.select.map(([e]) => [e.op, e.value]),
    [
      ["lit", null],
      ["lit", null],
      ["lit", false],
      ["lit", true],
      ["lit", null],
      ["lit", 7],
      ["lit", true],
    ],
  );
});
test("tautologies fold only for provably non-null operands", () => {
  const p = optimize(
    plan(
      "SELECT l.k = l.k AS a, r.id = r.id AS b, l.k IS NOT NULL AS c, r.v IS NULL AS d, l.k > 1 OR NOT l.k > 1 AS e, r.v > 1 OR NOT r.v > 1 AS f, COALESCE(NULL, l.k, 5) AS g FROM l LEFT JOIN r ON l.id = r.id",
    ),
  );
  assert.deepEqual(
    p.query.select.map(([e]) => [e.op, e.value]),
    [
      ["lit", true],
      ["=", undefined],
      ["lit", true],
      ["IS NULL", undefined],
      ["lit", true],
      ["OR", undefined],
      ["col", ["l", "k"]],
    ],
  );
});
test("inside its own ON clause the right table is not yet padded", () => {
  const p = optimize(plan("SELECT l.id AS id FROM l LEFT JOIN s ON s.id = s.id LEFT JOIN r ON s.id = s.id"));
  assert.deepEqual(p.query.joins[0], { op: "lit", value: true, args: [], type: "bool", nullable: false });
  assert.equal(p.query.joins[1].op, "=");
});
test("optimized and unoptimized execution agree", () => {
  const sqls = [
    "SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.v > 15",
    "SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE r.v IS NULL AND l.id > 1",
    "SELECT l.id AS id, r.v AS v FROM l LEFT JOIN r ON l.id = r.id WHERE COALESCE(r.v, 0) = 0",
    "SELECT l.id AS id, s.name AS name FROM l LEFT JOIN r ON l.id = r.id LEFT JOIN s ON r.v = s.id WHERE s.name <> 'ten'",
    "SELECT l.id AS id, s.name AS name FROM l LEFT JOIN r ON l.id = r.id LEFT JOIN s ON r.v = s.id WHERE s.name IS NULL",
    "SELECT l.id AS id, COUNT(r.v) AS n, SUM(r.v) AS t FROM l LEFT JOIN r ON l.id = r.id GROUP BY l.id HAVING SUM(r.v) > 5 OR SUM(r.v) IS NULL",
    "SELECT r.id AS id, r.v AS v FROM r ORDER BY id DESC NULLS LAST, v ASC NULLS FIRST",
    "SELECT l.id AS id FROM l WHERE l.k > 1 OR NOT l.k > 1",
    "SELECT r.id AS id FROM r WHERE r.v > 1 OR NOT r.v > 1",
    "SELECT r.id AS id FROM r WHERE r.id = r.id",
  ];
  for (const sql of sqls) assert.deepEqual(run(sql, true), run(sql, false), sql);
});
