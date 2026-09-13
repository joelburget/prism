/** Local scaling probe, including initialization; no acceptance threshold.
 * Run: node starter/benchmark_views.ts */
import assert from "node:assert/strict";
import { performance } from "node:perf_hooks";
import { commands } from "./views.ts";

for (const size of [1000, 10000]) {
  const database = ["a", "b"].map(name => ({ name,
    columns: ["k", "v"].map(name => ({ name, type: "int", nullable: false })),
    rows: Array.from({ length: size }, (_, k) => [k, 1]),
  }));
  const sqls = [
    "SELECT COUNT(*) AS n, SUM(v) AS s FROM a",
    "SELECT COUNT(*) AS n, SUM(a.v) AS s FROM a LEFT JOIN b ON a.k = b.k",
    "SELECT SUM(v) AS s FROM b",
  ];
  const create = sqls.flatMap((sql, i) => [false, true].map((optimize, j) => ({ op: "create", view: `v${i}-${j}`, sql, optimize })));
  const updates = Array.from({ length: 800 }, (_, i) => [
    { op: "apply", changes: [{ op: "update", table: "a", id: 1, row: [0, i % 2 + 1] }] },
    { op: "read", view: "v1-1" },
  ]).flat();
  const durations = [];
  for (let trial = 0; trial < 5; trial++) {
    const start = performance.now();
    const result = commands({ protocol_version: 1, task: "query-null", input: { database, commands: [...create, ...updates] } });
    durations.push(performance.now() - start);
    assert.deepEqual(result.at(-1), { ok: true, result: { revision: 800, columns: ["n", "s"], rows: [[size, size + 1]] } });
  }
  durations.sort((a, b) => a - b);
  console.log(`${size * 2} base rows, six views, 800 applies + reads: ${durations[2].toFixed(1)} ms median including creation`);
}
