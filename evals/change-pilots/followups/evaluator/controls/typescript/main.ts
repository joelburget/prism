// Scaling control for the documented unique-key aggregate workload only.
import { readFileSync } from 'node:fs';
const incremental = process.argv[2] === 'incremental';
const data = JSON.parse(readFileSync(0, 'utf8')).input;
const tables = new Map<string, number[][]>(data.database.map((t: any) => [t.name, t.rows]));
const left = tables.get('l')!.map(r => r[0]);
const right = new Map<number, number[]>(tables.get('r')!.map((r, i) => [i + 1, r]));
if (!left.every((k, i) => k === i) || ![...right.values()].every((r, i) => r[0] === left[i])) throw Error('outside control workload');
function recompute(): number {
  const index = new Map<number, number>();
  for (const r of right.values()) index.set(r[0], r[1]);
  let sum = 0;
  for (const key of left) sum += index.get(key)!;
  return sum;
}
let total = recompute(), revision = 0;
const results: any[] = [];
for (const c of data.commands) {
  let value: any;
  if (c.op === 'create') value = { view: c.view, revision };
  else if (c.op === 'apply') {
    for (const change of c.changes) {
      if (change.op !== 'update') throw Error('outside control workload');
      if (change.table === 'r') {
        const old = right.get(change.id)!;
        if (old[0] !== change.row[0]) throw Error('outside control workload');
        total += change.row[1] - old[1];
        right.set(change.id, change.row);
      } else if (change.table !== 'u') throw Error('outside control workload');
    }
    revision++;
    value = { revision };
  } else if (c.op === 'read') value = { revision, columns: ['n','s'], rows: [[left.length, incremental ? total : recompute()]] };
  else throw Error('outside control workload');
  results.push({ ok: true, result: value });
}
console.log(JSON.stringify({ ok: true, result: { results } }));
