/**
 * Incrementally maintained views over identified base rows.
 *
 * A view owns a private pipeline mirroring the batch executor: per-source
 * filtered scans, a chain of left-deep join levels, and an output stage that
 * either projects rows or maintains grouped aggregates. Every base-row change
 * flows through the pipeline as an exact retraction of the old row followed by
 * an exact insertion of the new one, so the maintained state always equals a
 * fresh evaluation of the query over the current base tables. Only views that
 * reference a changed table are touched.
 *
 * Every derived row carries an order key: the encounter position of each
 * contributing base row (0 for LEFT JOIN padding). Nested-loop encounter order
 * is exactly lexicographic order of these keys, so output ordering, DISTINCT's
 * first occurrence, and a group's first-encounter position can all be
 * reconstructed from keys instead of from evaluation order.
 */
import { DomainError, aggregates, requireThat as need, typed } from "./model.ts";
import type { Column, Expr, Plan, Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind, walk } from "./binder.ts";
import { optimize, evaluate, conjuncts, compareRows, same } from "./engine.ts";
const BOUND = 1_000_000_000;
const MAX_ID = 2_147_483_647;
interface Stored {
  pos: number;
  row: Value[];
}
interface BaseTable {
  name: string;
  columns: Column[];
  /** Live rows by private ID. `pos` is the encounter position (never reused). */
  rows: Map<number, Stored>;
  used: Set<number>;
  nextPos: number;
}
/** A row produced by a join level: one position per source (0 = padding). */
interface Derived {
  key: number[];
  row: Value[];
}
interface Sink {
  add(d: Derived): void;
  remove(d: Derived): void;
}
const keyOf = (k: number[]): string => k.join(",");
function compareKeys(a: number[], b: number[]): number {
  for (let i = 0; i < a.length && i < b.length; i++)
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return a.length - b.length;
}
function addTo<K, V>(index: Map<K, Set<V>>, k: K, v: V): void {
  const s = index.get(k);
  if (s) s.add(v);
  else index.set(k, new Set([v]));
}
function removeFrom<K, V>(index: Map<K, Set<V>>, k: K, v: V): void {
  const s = index.get(k);
  if (!s) return;
  s.delete(v);
  if (!s.size) index.delete(k);
}
const nulls = (n: number): Value[] => Array<Value>(n).fill(null);
/** Equality conjuncts of an ON clause, split into the side each one reads. */
interface Equi {
  lefts: Expr[];
  rights: Expr[];
}
function equiOf(on: Expr, source: number): Equi | null {
  const equi: Equi = { lefts: [], rights: [] };
  for (const c of conjuncts(on)) {
    if (c.op !== "=") continue;
    const sides = c.args.map((a) => {
      const s = new Set([...walk(a)].filter((n) => n.op === "col").map((n) => n.source!));
      if (s.size === 0 || [...s].some((x) => x > source)) return "none";
      return s.has(source) ? (s.size === 1 ? "right" : "none") : "left";
    });
    if (sides[0] === "left" && sides[1] === "right") {
      equi.lefts.push(c.args[0]);
      equi.rights.push(c.args[1]);
    } else if (sides[0] === "right" && sides[1] === "left") {
      equi.lefts.push(c.args[1]);
      equi.rights.push(c.args[0]);
    }
  }
  return equi.lefts.length ? equi : null;
}
/** Hash key of an equality tuple, or null when any component is NULL (which can never match). */
function equiKey(exprs: Expr[], row: Value[]): string | null {
  const values: Value[] = [];
  for (const e of exprs) {
    const v = evaluate(e, row);
    if (v === null) return null;
    values.push(v);
  }
  return JSON.stringify(values);
}
interface Prefix {
  d: Derived;
  lkey: string | null;
  /** Positions of right rows currently matched. Padding exists iff empty and outer. */
  matched: Set<number>;
}
/**
 * One join level: joins the rows of the previous level (prefixes) with this
 * source's filtered scan, maintaining match counts so LEFT JOIN padding is
 * added exactly when the last match disappears and retracted when the first
 * match appears. Equality conjuncts drive hash lookups on both sides; any
 * remaining conjuncts are checked by evaluating the full ON expression.
 */
class JoinLevel implements Sink {
  prefixes = new Map<string, Prefix>();
  scan = new Map<number, Stored & { rkey: string | null }>();
  leftIndex = new Map<string, Set<string>>();
  rightIndex = new Map<string, Set<number>>();
  rightMatches = new Map<number, Set<string>>();
  on: Expr;
  outer: boolean;
  equi: Equi | null;
  offset: number;
  width: number;
  next: Sink;
  constructor(on: Expr, outer: boolean, equi: Equi | null, offset: number, width: number, next: Sink) {
    this.on = on;
    this.outer = outer;
    this.equi = equi;
    this.offset = offset;
    this.width = width;
    this.next = next;
  }
  padded(p: Derived): Derived {
    return { key: [...p.key, 0], row: p.row.concat(nulls(this.width)) };
  }
  add(p: Derived): void {
    const pk = keyOf(p.key);
    const lkey = this.equi ? equiKey(this.equi.lefts, p.row) : null;
    const entry: Prefix = { d: p, lkey, matched: new Set() };
    this.prefixes.set(pk, entry);
    if (lkey !== null) addTo(this.leftIndex, lkey, pk);
    const candidates: Iterable<number> = !this.equi
      ? this.scan.keys()
      : lkey === null
        ? []
        : (this.rightIndex.get(lkey) ?? []);
    for (const pos of [...candidates]) {
      const row = p.row.concat(this.scan.get(pos)!.row);
      if (evaluate(this.on, row) !== true) continue;
      entry.matched.add(pos);
      addTo(this.rightMatches, pos, pk);
      this.next.add({ key: [...p.key, pos], row });
    }
    if (!entry.matched.size && this.outer) this.next.add(this.padded(p));
  }
  remove(p: Derived): void {
    const pk = keyOf(p.key);
    const entry = this.prefixes.get(pk)!;
    this.prefixes.delete(pk);
    if (entry.lkey !== null) removeFrom(this.leftIndex, entry.lkey, pk);
    for (const pos of entry.matched) {
      removeFrom(this.rightMatches, pos, pk);
      this.next.remove({ key: [...p.key, pos], row: p.row.concat(this.scan.get(pos)!.row) });
    }
    if (!entry.matched.size && this.outer) this.next.remove(this.padded(p));
  }
  insertRight(pos: number, row: Value[]): void {
    const rkey = this.equi ? equiKey(this.equi.rights, nulls(this.offset).concat(row)) : null;
    this.scan.set(pos, { pos, row, rkey });
    if (rkey !== null) addTo(this.rightIndex, rkey, pos);
    const candidates: Iterable<string> = !this.equi
      ? this.prefixes.keys()
      : rkey === null
        ? []
        : (this.leftIndex.get(rkey) ?? []);
    for (const pk of [...candidates]) {
      const entry = this.prefixes.get(pk)!;
      const full = entry.d.row.concat(row);
      if (evaluate(this.on, full) !== true) continue;
      if (!entry.matched.size && this.outer) this.next.remove(this.padded(entry.d));
      entry.matched.add(pos);
      addTo(this.rightMatches, pos, pk);
      this.next.add({ key: [...entry.d.key, pos], row: full });
    }
  }
  deleteRight(pos: number): void {
    const r = this.scan.get(pos)!;
    this.scan.delete(pos);
    if (r.rkey !== null) removeFrom(this.rightIndex, r.rkey, pos);
    const pks = this.rightMatches.get(pos) ?? new Set<string>();
    this.rightMatches.delete(pos);
    for (const pk of pks) {
      const entry = this.prefixes.get(pk)!;
      entry.matched.delete(pos);
      this.next.remove({ key: [...entry.d.key, pos], row: entry.d.row.concat(r.row) });
      if (!entry.matched.size && this.outer) this.next.add(this.padded(entry.d));
    }
  }
}
interface Unit {
  order: number[];
  out: Value[];
}
/** Running state of one aggregate within one group. */
interface AggState {
  n: number;
  sum: number;
  value: Value;
  /** A deleted value equalled the MIN/MAX; recompute from members before use. */
  stale: boolean;
}
interface Group {
  members: Map<string, { order: number[]; row: Value[] }>;
  states: AggState[];
  minOrder: number[] | null;
  minStale: boolean;
  /** Projected row, or null when HAVING rejects the group (or it is empty). */
  out: Value[] | null;
}
/**
 * Final stage: WHERE, then either projection of each surviving row or
 * maintenance of groups with incremental COUNT/SUM/MIN/MAX. Aggregate nodes in
 * SELECT/HAVING are rewritten to read slots appended after the source columns,
 * so the ordinary evaluator computes expressions over maintained values.
 */
class Output implements Sink {
  results = new Map<string, Unit>();
  groups = new Map<string, Group>();
  dirty = new Set<Group>();
  aggs: Expr[] = [];
  select: Expr[];
  having?: Expr;
  plan: Plan;
  width: number;
  invalidate: () => void;
  constructor(plan: Plan, width: number, invalidate: () => void) {
    this.plan = plan;
    this.width = width;
    this.invalidate = invalidate;
    const rewrite = (e: Expr): Expr => {
      if (aggregates.has(e.op)) {
        let j = this.aggs.findIndex((a) => same(a, e));
        if (j < 0) j = this.aggs.push(e) - 1;
        return { op: "col", index: width + j, args: [], type: e.type, nullable: e.nullable };
      }
      return { ...e, args: e.args.map(rewrite) };
    };
    this.select = plan.query.select.map(([e]) => rewrite(e));
    if (plan.query.having) this.having = rewrite(plan.query.having);
    if (plan.aggregate && !plan.query.groups.length) {
      this.groups.set("[]", this.newGroup());
      this.dirty.add(this.groups.get("[]")!);
    }
  }
  newGroup(): Group {
    return {
      members: new Map(),
      states: this.aggs.map(() => ({ n: 0, sum: 0, value: null, stale: false })),
      minOrder: null,
      minStale: false,
      out: null,
    };
  }
  groupKey(row: Value[]): string {
    return JSON.stringify(this.plan.query.groups.map((e) => evaluate(e, row)));
  }
  add(d: Derived): void {
    const q = this.plan.query;
    if (q.where && evaluate(q.where, d.row) !== true) return;
    this.invalidate();
    if (!this.plan.aggregate) {
      this.results.set(keyOf(d.key), {
        order: d.key,
        out: this.select.map((e) => evaluate(e, d.row)),
      });
      return;
    }
    const gk = this.groupKey(d.row);
    let g = this.groups.get(gk);
    if (!g) this.groups.set(gk, (g = this.newGroup()));
    g.members.set(keyOf(d.key), { order: d.key, row: d.row });
    if (g.minOrder === null || compareKeys(d.key, g.minOrder) < 0) g.minOrder = d.key;
    this.aggs.forEach((a, j) => {
      const s = g.states[j];
      if (!a.args.length) {
        s.n++;
        return;
      }
      const v = evaluate(a.args[0], d.row);
      if (v === null) return;
      s.n++;
      if (a.op === "SUM") s.sum += v as number;
      else if (a.op !== "COUNT" && !s.stale && (s.value === null || (a.op === "MIN" ? v < s.value : v > s.value)))
        s.value = v;
    });
    this.dirty.add(g);
  }
  remove(d: Derived): void {
    const dk = keyOf(d.key);
    if (!this.plan.aggregate) {
      if (this.results.delete(dk)) this.invalidate();
      return;
    }
    const g = this.groups.get(this.groupKey(d.row));
    if (!g || !g.members.delete(dk)) return;
    this.invalidate();
    if (g.minOrder !== null && compareKeys(d.key, g.minOrder) === 0) g.minStale = true;
    this.aggs.forEach((a, j) => {
      const s = g.states[j];
      if (!a.args.length) {
        s.n--;
        return;
      }
      const v = evaluate(a.args[0], d.row);
      if (v === null) return;
      s.n--;
      if (a.op === "SUM") s.sum -= v as number;
      else if (a.op !== "COUNT" && v === s.value) s.stale = true;
      if (!s.n) {
        s.value = null;
        s.stale = false;
      }
    });
    this.dirty.add(g);
  }
  /** Recompute output rows of groups touched by the batch (revisiting a group only for stale MIN/MAX or order). */
  finish(): void {
    for (const g of this.dirty) {
      if (!g.members.size && this.plan.query.groups.length) {
        for (const [k, v] of this.groups) if (v === g) this.groups.delete(k);
        continue;
      }
      if (g.minStale) {
        g.minOrder = null;
        for (const m of g.members.values())
          if (g.minOrder === null || compareKeys(m.order, g.minOrder) < 0) g.minOrder = m.order;
        g.minStale = false;
      }
      const values = this.aggs.map((a, j): Value => {
        const s = g.states[j];
        if (a.op === "COUNT") return s.n;
        if (a.op === "SUM") return s.n ? s.sum : null;
        if (s.stale) {
          s.value = null;
          for (const m of g.members.values()) {
            const v = evaluate(a.args[0], m.row);
            if (v !== null && (s.value === null || (a.op === "MIN" ? v < s.value : v > s.value)))
              s.value = v;
          }
          s.stale = false;
        }
        return s.value;
      });
      const representative = g.members.values().next().value?.row ?? nulls(this.width);
      const ext = representative.concat(values);
      g.out =
        this.having && evaluate(this.having, ext) !== true
          ? null
          : this.select.map((e) => evaluate(e, ext));
    }
    this.dirty.clear();
  }
  units(): Unit[] {
    if (!this.plan.aggregate) return [...this.results.values()];
    const out: Unit[] = [];
    for (const g of this.groups.values())
      if (g.out) out.push({ order: g.minOrder ?? [], out: g.out });
    return out;
  }
}
class View {
  plan: Plan;
  /** Source indexes by base table name (a table may appear more than once). */
  sourcesOf = new Map<string, number[]>();
  offsets: number[] = [];
  scan0 = new Map<number, Value[]>();
  levels: (JoinLevel | null)[] = [];
  output: Output;
  cache: { columns: string[]; rows: Value[][] } | null = null;
  constructor(plan: Plan) {
    this.plan = plan;
    const q = plan.query;
    let width = 0;
    plan.tables.forEach((t, s) => {
      this.offsets.push(width);
      width += t.columns.length;
      const name = q.sources[s][0];
      this.sourcesOf.set(name, [...(this.sourcesOf.get(name) ?? []), s]);
    });
    this.output = new Output(plan, width, () => (this.cache = null));
    // Build levels back to front so each one knows its downstream sink.
    let next: Sink = this.output;
    for (let s = plan.tables.length - 1; s >= 1; s--) {
      const level = new JoinLevel(
        q.joins[s - 1],
        q.outer[s - 1],
        equiOf(q.joins[s - 1], s),
        this.offsets[s],
        plan.tables[s].columns.length,
        next,
      );
      this.levels[s] = level;
      next = level;
    }
    this.levels[0] = null;
    this.first = next;
  }
  first: Sink;
  passes(s: number, row: Value[]): boolean {
    const padded = nulls(this.offsets[s]).concat(row);
    return this.plan.filters[s].every((p) => evaluate(p, padded) === true);
  }
  /** Retract `oldRow` (if it was live) and insert `newRow` (if any) at every source reading `table`. */
  change(table: string, pos: number, oldRow: Value[] | null, newRow: Value[] | null): void {
    for (const s of this.sourcesOf.get(table) ?? []) {
      if (s === 0) {
        if (oldRow && this.scan0.has(pos)) {
          this.first.remove({ key: [pos], row: this.scan0.get(pos)! });
          this.scan0.delete(pos);
        }
        if (newRow && this.passes(0, newRow)) {
          this.scan0.set(pos, newRow);
          this.first.add({ key: [pos], row: newRow });
        }
      } else {
        const level = this.levels[s]!;
        if (oldRow && level.scan.has(pos)) level.deleteRight(pos);
        if (newRow && this.passes(s, newRow)) level.insertRight(pos, newRow);
      }
    }
  }
  finish(): void {
    this.output.finish();
  }
  read(): { columns: string[]; rows: Value[][] } {
    if (!this.cache) {
      const q = this.plan.query;
      let units = this.output.units();
      if (q.distinct) {
        // Keep the earliest encounter of each projected row, as first-occurrence DISTINCT does.
        const seen = new Map<string, Unit>();
        for (const u of units) {
          const k = JSON.stringify(u.out);
          const prior = seen.get(k);
          if (!prior || compareKeys(u.order, prior.order) < 0) seen.set(k, u);
        }
        units = [...seen.values()];
      }
      const cmp = compareRows(q.order);
      units.sort((a, b) => cmp(a.out, b.out) || compareKeys(a.order, b.order));
      this.cache = {
        columns: q.select.map(([, a]) => a),
        rows: units
          .slice(q.offset, q.limit === undefined ? undefined : q.offset + q.limit)
          .map((u) => u.out),
      };
    }
    return { columns: [...this.cache.columns], rows: this.cache.rows.map((r) => [...r]) };
  }
}
// ---------------------------------------------------------------- commands
type Reply = { ok: true; result: unknown } | { ok: false; error: { code: string } };
function command(x: unknown, names: string[]): asserts x is Record<string, unknown> {
  need(x !== null && typeof x === "object" && !Array.isArray(x), "INVALID_COMMAND");
  const o = x as Record<string, unknown>;
  need(
    Object.keys(o).length === names.length && names.every((k) => Object.hasOwn(o, k)),
    "INVALID_COMMAND",
  );
}
const VIEW_NAME = /^[a-z][a-z0-9-]{0,39}$/;
const TABLE_NAME = /^[a-z_][a-z0-9_]*$/;
interface Change {
  op: "insert" | "update" | "delete";
  table: string;
  id: number;
  row?: unknown[];
}
function checkChanges(changes: unknown): Change[] {
  need(Array.isArray(changes) && changes.length > 0 && changes.length <= 200, "INVALID_COMMAND");
  return changes.map((c): Change => {
    need(c !== null && typeof c === "object" && !Array.isArray(c), "INVALID_COMMAND");
    const op = (c as Record<string, unknown>).op;
    need(op === "insert" || op === "update" || op === "delete", "INVALID_COMMAND");
    command(c, op === "delete" ? ["op", "table", "id"] : ["op", "table", "id", "row"]);
    need(typeof c.table === "string" && TABLE_NAME.test(c.table), "INVALID_COMMAND");
    need(
      typeof c.id === "number" && Number.isInteger(c.id) && c.id >= 1 && c.id <= MAX_ID,
      "INVALID_COMMAND",
    );
    if (op !== "delete") need(Array.isArray(c.row), "INVALID_COMMAND");
    return { op, table: c.table, id: c.id, row: c.row as unknown[] | undefined };
  });
}
function checkRow(t: BaseTable, row: unknown[]): Value[] {
  need(row.length === t.columns.length, "INVALID_ROW");
  row.forEach((v, i) => {
    const c = t.columns[i];
    need(v === null ? c.nullable : typed(v, c.type), "INVALID_ROW");
    need(c.type !== "int" || Math.abs(v as number) <= BOUND, "INVALID_ROW");
  });
  return row as Value[];
}
export class Database {
  tables = new Map<string, BaseTable>();
  schemas = new Map<string, Table>();
  views = new Map<string, View>();
  revision = 0;
  constructor(database: Map<string, Table>) {
    for (const [name, t] of database) {
      this.tables.set(name, {
        name,
        columns: t.columns,
        rows: new Map(t.rows.map((row, i) => [i + 1, { pos: i + 1, row }])),
        used: new Set(t.rows.map((_, i) => i + 1)),
        nextPos: t.rows.length,
      });
      // Views bind against schemas only; row data lives in the base tables.
      this.schemas.set(name, { name, columns: t.columns, rows: [] });
    }
  }
  run(commands: unknown[]): Reply[] {
    return commands.map((c) => {
      try {
        return { ok: true, result: this.command(c) };
      } catch (e) {
        if (!(e instanceof DomainError)) throw e;
        return { ok: false, error: { code: e.message } };
      }
    });
  }
  command(c: unknown): unknown {
    need(c !== null && typeof c === "object" && !Array.isArray(c), "INVALID_COMMAND");
    const op = (c as Record<string, unknown>).op;
    switch (op) {
      case "create": {
        command(c, ["op", "view", "sql", "optimize"]);
        need(typeof c.view === "string" && VIEW_NAME.test(c.view), "INVALID_COMMAND");
        need(typeof c.sql === "string" && typeof c.optimize === "boolean", "INVALID_COMMAND");
        need(!this.views.has(c.view), "VIEW_EXISTS");
        let plan = bind(new Parser(c.sql).parse(), this.schemas);
        if (c.optimize) plan = optimize(plan);
        const view = new View(plan);
        for (const t of new Set(plan.query.sources.map(([n]) => n)))
          for (const s of this.tables.get(t)!.rows.values()) view.change(t, s.pos, null, s.row);
        view.finish();
        this.views.set(c.view, view);
        return { view: c.view, revision: this.revision };
      }
      case "read":
      case "drop": {
        command(c, ["op", "view"]);
        need(typeof c.view === "string" && VIEW_NAME.test(c.view), "INVALID_COMMAND");
        const view = this.views.get(c.view);
        need(view, "UNKNOWN_VIEW");
        if (op === "drop") {
          this.views.delete(c.view);
          return { dropped: c.view };
        }
        return { revision: this.revision, ...view.read() };
      }
      case "apply": {
        command(c, ["op", "changes"]);
        this.apply(checkChanges(c.changes));
        return { revision: this.revision };
      }
      default:
        throw new DomainError("INVALID_COMMAND");
    }
  }
  /** Validate every change against a private overlay, then commit all at once. */
  apply(changes: Change[]): void {
    const overlay = new Map<string, Map<number, Value[] | null>>();
    const reserved = new Map<string, Set<number>>();
    for (const ch of changes) {
      const t = this.tables.get(ch.table);
      need(t, "UNKNOWN_TABLE");
      const row = ch.op === "delete" ? null : checkRow(t, ch.row!);
      let pending = overlay.get(ch.table);
      if (!pending) overlay.set(ch.table, (pending = new Map()));
      const exists = pending.has(ch.id) ? pending.get(ch.id) !== null : t.rows.has(ch.id);
      if (ch.op === "insert") {
        need(!t.used.has(ch.id) && !reserved.get(ch.table)?.has(ch.id), "ROW_ID_USED");
        addTo(reserved, ch.table, ch.id);
      } else need(exists, "UNKNOWN_ROW");
      pending.set(ch.id, row);
    }
    for (const [name, ids] of reserved) for (const id of ids) this.tables.get(name)!.used.add(id);
    const affected = [...this.views.values()].filter((v) =>
      [...overlay.keys()].some((t) => v.sourcesOf.has(t)),
    );
    for (const [name, pending] of overlay) {
      const t = this.tables.get(name)!;
      for (const [id, row] of pending) {
        const old = t.rows.get(id);
        let pos: number;
        if (row === null) {
          if (!old) continue; // inserted and deleted within the batch
          t.rows.delete(id);
          pos = old.pos;
        } else if (old) {
          if (JSON.stringify(old.row) === JSON.stringify(row)) continue;
          old.row = row;
          pos = old.pos;
        } else {
          pos = ++t.nextPos;
          t.rows.set(id, { pos, row });
        }
        for (const v of affected) v.change(name, pos, old?.row ?? null, row);
      }
    }
    for (const v of affected) v.finish();
    this.revision++;
  }
}
