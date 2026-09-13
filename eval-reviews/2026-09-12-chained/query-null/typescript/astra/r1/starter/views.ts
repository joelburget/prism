/** Incremental materialized views. Each join retains its prefix relation and
 * equality indexes; deltas invalidate only affected left prefixes. Group state
 * retracts/adds individual bag contributions, independently of output windows. */
import { aggregates, DomainError, requireThat as need, validate } from "./model.ts";
import type { Expr, Plan, Table, Value } from "./model.ts";
import { bind, walk } from "./binder.ts";
import { Parser } from "./parser.ts";
import { evaluate, finish, optimize } from "./engine.ts";

type RecordRow = { key: string; row: Value[]; order: number[] };
type Relation = Map<string, RecordRow>;
type Delta = Map<string, { before?: RecordRow; after?: RecordRow }>;
type Base = { table: Table; rows: Map<number, RecordRow>; used: Set<number>; next: number };
function compare(a: RecordRow, b: RecordRow): number {
  for (let i = 0; i < a.order.length; i++) {
    if (a.order[i] !== b.order[i]) return a.order[i] - b.order[i];
  }
  return 0;
}
function same(a: RecordRow, b: RecordRow): boolean {
  return a.row.length === b.row.length && a.row.every((v, i) => v === b.row[i]);
}
function conjunctions(e: Expr): Expr[] {
  return e.op === "AND" ? e.args.flatMap(conjunctions) : [e];
}
class Index {
  buckets = new Map<string, Set<string>>();
  readonly key: (r: RecordRow) => string | undefined;
  constructor(key: (r: RecordRow) => string | undefined) { this.key = key; }
  add(r: RecordRow): void {
    const k = this.key(r);
    if (k === undefined) return;
    let bucket = this.buckets.get(k);
    if (!bucket) this.buckets.set(k, bucket = new Set());
    bucket.add(r.key);
  }
  remove(r: RecordRow): void {
    const k = this.key(r);
    if (k === undefined) return;
    const bucket = this.buckets.get(k)!;
    bucket.delete(r.key);
    if (!bucket.size) this.buckets.delete(k);
  }
  get(k: string | undefined): Iterable<string> {
    return k === undefined ? [] : this.buckets.get(k) ?? [];
  }
}
class Join {
  left: Relation = new Map();
  right: Relation = new Map();
  output: Relation = new Map();
  byLeft = new Map<string, Relation>();
  leftIndex: Index;
  rightIndex: Index;
  readonly plan: Plan;
  readonly source: number;
  constructor(plan: Plan, source: number, offset: number) {
    this.plan = plan;
    this.source = source;
    const pairs: [Expr, Expr][] = [];
    for (const e of conjunctions(plan.query.joins[source - 1])) {
      if (e.op !== "=") continue;
      const sources = e.args.map(a => [...walk(a)].filter(n => n.op === "col").map(n => n.source!));
      for (const [l, r] of [[0, 1], [1, 0]]) {
        if (sources[l].length && sources[l].every(s => s < source) &&
            sources[r].length && sources[r].every(s => s === source)) {
          pairs.push([e.args[l], e.args[r]]);
          break;
        }
      }
    }
    const key = (r: RecordRow, side: number): string | undefined => {
      const row = side ? [...Array<Value>(offset), ...r.row] : r.row;
      const values = pairs.map(p => evaluate(p[side], row));
      return values.some(v => v === null) ? undefined : JSON.stringify(values);
    };
    // Without an equality conjunct, the single bucket is the general theta-join
    // fallback. Residual predicates are always evaluated, including in indexes.
    this.leftIndex = new Index(r => key(r, 0));
    this.rightIndex = new Index(r => key(r, 1));
  }
  update(leftDelta: Delta, rightDelta: Delta): Delta {
    const affected = new Set(leftDelta.keys());
    for (const d of rightDelta.values()) {
      for (const r of [d.before, d.after]) if (r) {
        for (const k of this.leftIndex.get(this.rightIndex.key(r))) affected.add(k);
      }
    }
    for (const [key, d] of leftDelta) {
      if (d.before) this.leftIndex.remove(d.before);
      this.left.delete(key);
      if (d.after) { this.left.set(key, d.after); this.leftIndex.add(d.after); }
    }
    for (const [key, d] of rightDelta) {
      if (d.before) this.rightIndex.remove(d.before);
      this.right.delete(key);
      if (d.after) { this.right.set(key, d.after); this.rightIndex.add(d.after); }
    }
    const delta: Delta = new Map();
    const q = this.plan.query;
    for (const key of affected) {
      const old = this.byLeft.get(key) ?? new Map<string, RecordRow>();
      const fresh: Relation = new Map();
      const l = this.left.get(key);
      if (l) {
        for (const rk of this.rightIndex.get(this.leftIndex.key(l))) {
          const r = this.right.get(rk)!;
          const combined = { key: `${l.key}/${r.key}`, row: [...l.row, ...r.row], order: [...l.order, ...r.order] };
          if (evaluate(q.joins[this.source - 1], combined.row) === true) fresh.set(combined.key, combined);
        }
        if (!fresh.size && q.leftJoins[this.source - 1]) {
          const padded = { key: `${l.key}/_`, row: [...l.row, ...Array<Value>(this.plan.tables[this.source].columns.length).fill(null)], order: [...l.order, -1] };
          fresh.set(padded.key, padded);
        }
      }
      for (const [k, before] of old) {
        const after = fresh.get(k);
        if (!after || !same(before, after)) delta.set(k, { before, after });
      }
      for (const [k, after] of fresh) if (!old.has(k)) delta.set(k, { after });
      if (l) this.byLeft.set(key, fresh); else this.byLeft.delete(key);
    }
    for (const [k, d] of delta) {
      if (d.after) this.output.set(k, d.after); else this.output.delete(k);
    }
    return delta;
  }
}

class Accumulator {
  count = 0;
  sum = 0;
  values = new Map<Value, number>();
  extreme: Value = null;
  dirty = false;
  readonly expr: Expr;
  constructor(expr: Expr) { this.expr = expr; }
  change(row: Value[], sign: number): void {
    const e = this.expr;
    const value = e.args.length ? evaluate(e.args[0], row) : 1;
    if (value === null) return;
    this.count += sign;
    if (e.op === "SUM") this.sum += sign * (value as number);
    if (e.op === "MIN" || e.op === "MAX") {
      const n = (this.values.get(value) ?? 0) + sign;
      if (n) this.values.set(value, n); else this.values.delete(value);
      if (sign < 0 && value === this.extreme && !n) this.dirty = true;
      if (sign > 0 && (this.extreme === null || (e.op === "MIN" ? value < this.extreme : value > this.extreme))) this.extreme = value;
    }
  }
  value(): Value {
    if (this.expr.op === "COUNT") return this.count;
    if (!this.count) return null;
    if (this.expr.op === "SUM") return this.sum;
    if (this.dirty) {
      this.extreme = null;
      for (const v of this.values.keys()) if (v !== null &&
          (this.extreme === null || (this.expr.op === "MIN" ? v < this.extreme : v > this.extreme))) this.extreme = v;
      this.dirty = false;
    }
    return this.extreme;
  }
}
/** Indexed min-heap keeps group encounter order under arbitrary retractions. */
class EncounterHeap {
  heap: RecordRow[] = [];
  positions = new Map<string, number>();
  swap(a: number, b: number): void {
    [this.heap[a], this.heap[b]] = [this.heap[b], this.heap[a]];
    this.positions.set(this.heap[a].key, a);
    this.positions.set(this.heap[b].key, b);
  }
  repair(index: number): void {
    while (index > 0) {
      const parent = (index - 1) >> 1;
      if (compare(this.heap[parent], this.heap[index]) <= 0) break;
      this.swap(parent, index);
      index = parent;
    }
    for (;;) {
      let child = index * 2 + 1;
      if (child >= this.heap.length) break;
      if (child + 1 < this.heap.length && compare(this.heap[child + 1], this.heap[child]) < 0) child++;
      if (compare(this.heap[index], this.heap[child]) <= 0) break;
      this.swap(index, child);
      index = child;
    }
  }
  add(r: RecordRow): void {
    this.positions.set(r.key, this.heap.length);
    this.heap.push(r);
    this.repair(this.heap.length - 1);
  }
  remove(key: string): void {
    const index = this.positions.get(key)!;
    const last = this.heap.pop()!;
    this.positions.delete(key);
    if (index < this.heap.length) {
      this.heap[index] = last;
      this.positions.set(last.key, index);
      this.repair(index);
    }
  }
}
class Group {
  members: Relation = new Map();
  encounter = new EncounterHeap();
  accumulators: Accumulator[];
  projected?: RecordRow;
  constructor(exprs: Expr[]) { this.accumulators = exprs.map(e => new Accumulator(e)); }
  change(r: RecordRow, sign: number): void {
    if (sign < 0) {
      this.members.delete(r.key);
      this.encounter.remove(r.key);
    } else {
      this.members.set(r.key, r);
      this.encounter.add(r);
    }
    for (const a of this.accumulators) a.change(r.row, sign);
  }
  refresh(plan: Plan): void {
    const first = this.encounter.heap[0];
    const cached = new Map(this.accumulators.map(a => [a.expr, a.value()]));
    const row = first?.row ?? [];
    const q = plan.query;
    this.projected = !q.having || evaluate(q.having, row, [], cached) === true
      ? { key: "", row: q.select.map(([e]) => evaluate(e, row, [], cached)), order: first?.order ?? [] }
      : undefined;
  }
}
class View {
  scans: Relation[];
  joins: Join[];
  groups = new Map<string, Group>();
  projected: Relation = new Map();
  aggregateExprs: Expr[];
  cached?: { columns: string[]; rows: Value[][] };
  readonly plan: Plan;
  constructor(plan: Plan, bases: Map<string, Base>) {
    this.plan = plan;
    this.scans = plan.tables.map(() => new Map());
    let offset = plan.tables[0].columns.length;
    this.joins = plan.tables.slice(1).map((t, i) => {
      const join = new Join(plan, i + 1, offset);
      offset += t.columns.length;
      return join;
    });
    const roots = [...plan.query.select.map(([e]) => e), ...(plan.query.having ? [plan.query.having] : [])];
    this.aggregateExprs = roots.flatMap(e => [...walk(e)]).filter(e => aggregates.has(e.op));
    if (plan.aggregate && !plan.query.groups.length) this.groups.set("[]", new Group(this.aggregateExprs));
    const initial = new Map<string, Delta>();
    for (const t of plan.tables) initial.set(t.name, new Map([...bases.get(t.name)!.rows.values()].map(r => [r.key, { after: r }])));
    this.update(initial);
  }
  update(changes: Map<string, Delta>): void {
    if (!this.plan.tables.some(t => changes.has(t.name))) return;
    this.cached = undefined;
    let offset = 0;
    const scans = this.plan.tables.map((t, source) => {
      const delta: Delta = new Map();
      for (const [key, d] of changes.get(t.name) ?? []) {
        const before = this.scans[source].get(key);
        const after = d.after && this.plan.filters[source].every(e => evaluate(e, [...Array<Value>(offset), ...d.after!.row]) === true) ? d.after : undefined;
        if (before || after) delta.set(key, { before, after });
        if (after) this.scans[source].set(key, after); else this.scans[source].delete(key);
      }
      offset += t.columns.length;
      return delta;
    });
    let delta = scans[0];
    this.joins.forEach((join, i) => { delta = join.update(delta, scans[i + 1]); });
    const q = this.plan.query;
    const touched = new Map<string, Group>();
    for (const [key, d] of delta) {
      for (const [r, sign] of [[d.before, -1], [d.after, 1]] as const) {
        if (!r || (q.where && evaluate(q.where, r.row) !== true)) continue;
        if (!this.plan.aggregate) {
          if (sign < 0) this.projected.delete(key);
          else this.projected.set(key, { ...r, row: q.select.map(([e]) => evaluate(e, r.row)) });
        } else {
          const k = JSON.stringify(q.groups.map(e => evaluate(e, r.row)));
          let g = this.groups.get(k);
          if (!g) this.groups.set(k, g = new Group(this.aggregateExprs));
          g.change(r, sign);
          touched.set(k, g);
        }
      }
    }
    // A global group also exists at creation on an empty relation.
    if (!q.groups.length && this.plan.aggregate) touched.set("[]", this.groups.get("[]")!);
    for (const [key, g] of touched) {
      if (q.groups.length && !g.members.size) this.groups.delete(key);
      else g.refresh(this.plan);
    }
  }
  read(): { columns: string[]; rows: Value[][] } {
    if (!this.cached) {
      const records = this.plan.aggregate
        ? [...this.groups.values()].flatMap(g => g.projected ? [g.projected] : [])
        : [...this.projected.values()];
      records.sort(compare);
      this.cached = finish(this.plan, records.map(r => r.row));
    }
    return this.cached;
  }
}

function shape(x: unknown, keys: string[]): asserts x is Record<string, unknown> {
  need(x !== null && typeof x === "object" && !Array.isArray(x) &&
    Object.keys(x).length === keys.length && keys.every(k => Object.hasOwn(x, k)), "INVALID_COMMAND");
}
function viewName(x: unknown): asserts x is string {
  need(typeof x === "string" && /^[a-z][a-z0-9-]{0,39}$/.test(x), "INVALID_COMMAND");
}
type Change = { op: "insert" | "update" | "delete"; table: string; id: number; row?: Value[] };
function validateChanges(value: unknown): Change[] {
  need(Array.isArray(value) && value.length > 0 && value.length <= 200, "INVALID_COMMAND");
  for (const c of value) {
    need(c !== null && typeof c === "object" && ["insert", "update", "delete"].includes(c.op), "INVALID_COMMAND");
    shape(c, c.op === "delete" ? ["op", "table", "id"] : ["op", "table", "id", "row"]);
    need(typeof c.table === "string" && /^[a-z_][a-z0-9_]*$/.test(c.table) &&
      typeof c.id === "number" && Number.isInteger(c.id) && c.id >= 1 && c.id <= 2147483647 &&
      (c.op === "delete" || Array.isArray(c.row)), "INVALID_COMMAND");
  }
  return value as Change[];
}
function validateRow(row: Value[], table: Table): void {
  need(row.length === table.columns.length && row.every((v, i) => {
    const c = table.columns[i];
    return v === null ? c.nullable : c.type === "int" ? typeof v === "number" && Number.isInteger(v) && Math.abs(v) <= 1000000000
      : typeof v === (c.type === "text" ? "string" : "boolean");
  }), "INVALID_ROW");
}
export function commands(request: Record<string, unknown>): unknown[] {
  const input = request.input as Record<string, unknown>;
  need(input !== null && typeof input === "object" && !Array.isArray(input) &&
    Object.keys(input).length === 2 && Object.hasOwn(input, "database") && Object.hasOwn(input, "commands") &&
    Array.isArray(input.commands) && input.commands.length <= 2000);
  const [database] = validate({ ...request, input: { database: input.database, queries: [] } });
  const bases = new Map<string, Base>();
  for (const [name, table] of database) bases.set(name, {
    table, rows: new Map(table.rows.map((row, i) => [i + 1, { key: String(i + 1), row, order: [i + 1] }])),
    used: new Set(table.rows.map((_, i) => i + 1)), next: table.rows.length + 1,
  });
  const views = new Map<string, View>();
  let revision = 0;
  return input.commands.map(command => {
    try {
      need(command !== null && typeof command === "object" && !Array.isArray(command), "INVALID_COMMAND");
      const c = command as Record<string, unknown>;
      let result: unknown;
      if (c.op === "apply") {
        shape(c, ["op", "changes"]);
        const changes = validateChanges(c.changes);
        // Per-ID overlay: validation never copies entire tables or used-ID sets.
        const staged = new Map<string, Map<number, RecordRow | undefined>>();
        const inserted = new Map<string, Set<number>>();
        const next = new Map<string, number>();
        for (const change of changes) {
          const b = bases.get(change.table);
          need(b, "UNKNOWN_TABLE");
          if (change.op !== "delete") validateRow(change.row!, b.table);
          let rows = staged.get(change.table);
          if (!rows) staged.set(change.table, rows = new Map());
          let used = inserted.get(change.table);
          if (!used) inserted.set(change.table, used = new Set());
          const old = rows.has(change.id) ? rows.get(change.id) : b.rows.get(change.id);
          if (change.op === "insert") {
            need(!b.used.has(change.id) && !used.has(change.id), "ROW_ID_USED");
            used.add(change.id);
            const position = next.get(change.table) ?? b.next;
            next.set(change.table, position + 1);
            rows.set(change.id, { key: String(change.id), row: change.row!, order: [position] });
          } else {
            need(old, "UNKNOWN_ROW");
            rows.set(change.id, change.op === "delete" ? undefined : { ...old, row: change.row! });
          }
        }
        const deltas = new Map<string, Delta>();
        for (const [name, rows] of staged) {
          const b = bases.get(name)!;
          const delta: Delta = new Map();
          for (const [id, after] of rows) {
            const before = b.rows.get(id);
            if ((before || after) && !(before && after && same(before, after))) delta.set(String(id), { before, after });
          }
          if (delta.size) deltas.set(name, delta);
        }
        // All domain-error checks have completed. Maintenance uses only the final
        // batch deltas, so simultaneous changes on both join sides count once.
        for (const view of views.values()) view.update(deltas);
        for (const [name, rows] of staged) {
          const b = bases.get(name)!;
          for (const [id, row] of rows) { if (row) b.rows.set(id, row); else b.rows.delete(id); }
          for (const id of inserted.get(name)!) b.used.add(id);
          b.next = next.get(name) ?? b.next;
        }
        result = { revision: ++revision };
      } else if (c.op === "create") {
        shape(c, ["op", "view", "sql", "optimize"]);
        viewName(c.view);
        need(typeof c.sql === "string" && typeof c.optimize === "boolean", "INVALID_COMMAND");
        need(!views.has(c.view), "VIEW_EXISTS");
        const plan = bind(new Parser(c.sql).parse(), database);
        const view = new View(c.optimize ? optimize(plan) : plan, bases);
        views.set(c.view, view);
        result = { view: c.view, revision };
      } else if (c.op === "read" || c.op === "drop") {
        shape(c, ["op", "view"]);
        viewName(c.view);
        const view = views.get(c.view);
        need(view, "UNKNOWN_VIEW");
        if (c.op === "drop") { views.delete(c.view); result = { dropped: c.view }; }
        else result = { revision, ...view.read() };
      } else throw new DomainError("INVALID_COMMAND");
      return { ok: true, result };
    } catch (e) {
      if (!(e instanceof DomainError)) throw e;
      return { ok: false, error: { code: e.message } };
    }
  });
}
