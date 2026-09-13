/**
 * Incrementally maintained views over the checkpoint-one logical plan.
 *
 * A view owns one bound (optionally optimized) plan and a chain of maintained
 * stages: per-source scans, one state per join, the WHERE survivors, and either
 * projected rows or aggregate groups. Every base change is turned into a small
 * delta that is pushed through those stages, so untouched rows, tuples and
 * groups are never revisited. Only ORDER BY / LIMIT is resolved at read time,
 * over the maintained result itself.
 */
import { aggregates } from "./model.ts";
import type { Expr, Plan, Value } from "./model.ts";
import { walk } from "./binder.ts";
import { compare, evaluate, holds, rank } from "./engine.ts";

/** A base row keeps its private identity and its stable encounter position. */
export interface BaseRow {
  id: number;
  seq: number;
  values: Value[];
}
export interface Delta<T> {
  removed: T[];
  added: T[];
}
const nothing: Delta<BaseRow> = { removed: [], added: [] };
/** A joined tuple; `seqs` is the encounter position of each source row. */
interface Tuple {
  key: string;
  seqs: number[];
  values: Value[];
}
/** Lexicographic encounter order; a padded source contributes -1. */
function earlier(a: number[], b: number[]): number {
  for (let i = 0; i < a.length; i++)
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
}
/**
 * Hash key of one join side. A NULL key component yields `null`, the bucket
 * that is stored but never probed, because NULL never equals NULL.
 */
function hashOf(keys: Expr[] | null, row: Value[]): string | null {
  if (!keys) return "";
  const vs: Value[] = [];
  for (const e of keys) {
    const v = evaluate(e, row);
    if (v === null) return null;
    vs.push(v);
  }
  return JSON.stringify(vs);
}
/** Bucketed membership index supporting removal by entry key. */
class Index<T> {
  buckets = new Map<string | null, Map<string, T>>();
  add(hash: string | null, key: string, value: T): void {
    let b = this.buckets.get(hash);
    if (!b) this.buckets.set(hash, (b = new Map()));
    b.set(key, value);
  }
  remove(hash: string | null, key: string): void {
    const b = this.buckets.get(hash);
    if (!b) return;
    b.delete(key);
    if (!b.size) this.buckets.delete(hash);
  }
  probe(hash: string | null): Iterable<T> {
    return hash === null ? [] : (this.buckets.get(hash)?.values() ?? []);
  }
}
/** Accumulates a stage delta, cancelling a removal of something just added. */
class Emit {
  removed = new Map<string, Tuple>();
  added = new Map<string, Tuple>();
  add(t: Tuple): void {
    this.added.set(t.key, t);
  }
  remove(t: Tuple): void {
    if (this.added.delete(t.key)) return;
    this.removed.set(t.key, t);
  }
  delta(): Delta<Tuple> {
    return {
      removed: [...this.removed.values()],
      added: [...this.added.values()],
    };
  }
}
interface LeftEntry {
  tuple: Tuple;
  /** Live matches of this left tuple, keyed by right row id. */
  matches: Map<number, Tuple>;
  /** The NULL-extended stand-in kept while an outer join has no match. */
  padded: Tuple | null;
}
/**
 * One join of the left-deep chain. The left input is the previous level, the
 * right input is a base scan. Both sides are indexed on the equality conjuncts
 * of the ON condition, so a delta on either side only visits candidate partners.
 */
class JoinLevel {
  lefts = new Map<string, LeftEntry>();
  leftIndex = new Index<LeftEntry>();
  rights = new Index<BaseRow>();
  pad: Value[];
  on: Expr;
  outer: boolean;
  leftWidth: number;
  leftKeys: Expr[] | null;
  rightKeys: Expr[] | null;
  constructor(
    on: Expr,
    outer: boolean,
    leftWidth: number,
    rightWidth: number,
    leftKeys: Expr[] | null,
    rightKeys: Expr[] | null,
  ) {
    this.on = on;
    this.outer = outer;
    this.leftWidth = leftWidth;
    this.leftKeys = leftKeys;
    this.rightKeys = rightKeys;
    this.pad = Array<Value>(rightWidth).fill(null);
  }
  /** Right-side expressions address whole-row slots, so the row is offset. */
  private rightHash(r: BaseRow): string | null {
    return hashOf(this.rightKeys, [
      ...Array<Value>(this.leftWidth),
      ...r.values,
    ]);
  }
  private combine(l: Tuple, r: BaseRow): Tuple | null {
    const values = [...l.values, ...r.values];
    if (!holds(this.on, values)) return null;
    return { key: `${l.key}/${r.id}`, seqs: [...l.seqs, r.seq], values };
  }
  private padding(l: Tuple): Tuple {
    return {
      key: `${l.key}/p`,
      seqs: [...l.seqs, -1],
      values: [...l.values, ...this.pad],
    };
  }
  step(left: Delta<Tuple>, right: Delta<BaseRow>): Delta<Tuple> {
    const out = new Emit();
    /** Retract whole left tuples first, then retract and add right rows
        against the surviving left side, and finally join new left tuples
        against the settled right side: every pair is produced exactly once. */
    for (const l of left.removed) {
      const e = this.lefts.get(l.key);
      if (!e) continue;
      this.lefts.delete(l.key);
      this.leftIndex.remove(hashOf(this.leftKeys, e.tuple.values), l.key);
      for (const t of e.matches.values()) out.remove(t);
      if (e.padded) out.remove(e.padded);
    }
    for (const r of right.removed) {
      const hash = this.rightHash(r);
      this.rights.remove(hash, String(r.id));
      for (const e of this.leftIndex.probe(hash)) {
        const t = e.matches.get(r.id);
        if (!t) continue;
        e.matches.delete(r.id);
        out.remove(t);
        /** Losing a left row's last match restores its padded stand-in. */
        if (!e.matches.size && this.outer)
          out.add((e.padded = this.padding(e.tuple)));
      }
    }
    for (const r of right.added) {
      const hash = this.rightHash(r);
      this.rights.add(hash, String(r.id), r);
      for (const e of this.leftIndex.probe(hash)) {
        const t = this.combine(e.tuple, r);
        if (!t) continue;
        if (e.padded) {
          out.remove(e.padded);
          e.padded = null;
        }
        e.matches.set(r.id, t);
        out.add(t);
      }
    }
    for (const l of left.added) {
      const e: LeftEntry = { tuple: l, matches: new Map(), padded: null };
      const hash = hashOf(this.leftKeys, l.values);
      this.lefts.set(l.key, e);
      this.leftIndex.add(hash, l.key, e);
      for (const r of this.rights.probe(hash)) {
        const t = this.combine(l, r);
        if (!t) continue;
        e.matches.set(r.id, t);
        out.add(t);
      }
      if (!e.matches.size && this.outer) out.add((e.padded = this.padding(l)));
    }
    return out.delta();
  }
}
interface Published {
  row: Value[];
  seqs: number[];
  bucket: string;
}
/**
 * The maintained result. Without DISTINCT each contribution is one output row;
 * with DISTINCT equal rows share a bucket that survives until its last
 * contribution leaves and reports the encounter position of its first one.
 */
class Output {
  entries = new Map<string, Published>();
  buckets = new Map<
    string,
    { row: Value[]; members: Set<string>; min: number[] }
  >();
  distinct: boolean;
  constructor(distinct: boolean) {
    this.distinct = distinct;
  }
  set(key: string, row: Value[] | null, seqs: number[]): void {
    const old = this.entries.get(key);
    if (old) {
      this.entries.delete(key);
      if (this.distinct) {
        const b = this.buckets.get(old.bucket)!;
        b.members.delete(key);
        if (!b.members.size) this.buckets.delete(old.bucket);
        else if (earlier(old.seqs, b.min) === 0) {
          /** The first occurrence left: the next one now represents it. */
          let min: number[] | null = null;
          for (const m of b.members) {
            const s = this.entries.get(m)!.seqs;
            if (!min || earlier(s, min) < 0) min = s;
          }
          b.min = min!;
        }
      }
    }
    if (!row) return;
    const bucket = this.distinct ? JSON.stringify(row) : key;
    this.entries.set(key, { row, seqs, bucket });
    if (!this.distinct) return;
    const b = this.buckets.get(bucket);
    if (!b)
      this.buckets.set(bucket, { row, members: new Set([key]), min: seqs });
    else {
      b.members.add(key);
      if (earlier(seqs, b.min) < 0) b.min = seqs;
    }
  }
  rows(): { row: Value[]; seqs: number[] }[] {
    return this.distinct
      ? [...this.buckets.values()].map((b) => ({ row: b.row, seqs: b.min }))
      : [...this.entries.values()];
  }
}
/** Incremental state of one aggregate slot inside one group. */
interface Slot {
  n: number;
  sum: number;
  best: Value;
}
/**
 * One GROUP BY group, or the single global group. Additions update every slot
 * in constant time; removing the current MIN/MAX, or the first-encountered
 * member, revisits this group alone.
 */
class Group {
  members = new Map<string, Tuple>();
  slots: Slot[];
  first: Tuple | null = null;
  aggs: Expr[];
  constructor(aggs: Expr[]) {
    this.aggs = aggs;
    this.slots = aggs.map(() => ({ n: 0, sum: 0, best: null }));
  }
  private argOf(i: number, t: Tuple): Value {
    const a = this.aggs[i].args[0];
    return a ? evaluate(a, t.values) : null;
  }
  private extreme(i: number, v: Value): boolean {
    const s = this.slots[i];
    return (
      s.best === null || rank(v, s.best) === (this.aggs[i].op === "MIN" ? -1 : 1)
    );
  }
  add(t: Tuple): void {
    this.members.set(t.key, t);
    this.aggs.forEach((e, i) => {
      const v = this.argOf(i, t);
      if (v === null) return;
      const s = this.slots[i];
      if (s.n++ === 0) s.best = null;
      if (e.op === "SUM") s.sum += v as number;
      else if ((e.op === "MIN" || e.op === "MAX") && this.extreme(i, v))
        s.best = v;
    });
    if (!this.first || earlier(t.seqs, this.first.seqs) < 0) this.first = t;
  }
  remove(t: Tuple): void {
    this.members.delete(t.key);
    const stale: number[] = [];
    this.aggs.forEach((e, i) => {
      const v = this.argOf(i, t);
      if (v === null) return;
      const s = this.slots[i];
      s.n--;
      if (e.op === "SUM") s.sum -= v as number;
      else if ((e.op === "MIN" || e.op === "MAX") && rank(v, s.best) === 0)
        stale.push(i);
    });
    const refirst = this.first !== null && this.first.key === t.key;
    if (!stale.length && !refirst) return;
    /** An extremum or the first member vanished: revisit this group only. */
    if (refirst) this.first = null;
    for (const i of stale) this.slots[i].best = null;
    for (const m of this.members.values()) {
      if (refirst && (!this.first || earlier(m.seqs, this.first.seqs) < 0))
        this.first = m;
      for (const i of stale) {
        const v = this.argOf(i, m);
        if (v !== null && this.extreme(i, v)) this.slots[i].best = v;
      }
    }
  }
  /** Maintained value of every aggregate, in the order evaluation expects. */
  values(): Value[] {
    return this.aggs.map((e, i) => {
      const s = this.slots[i];
      if (e.op === "COUNT") return e.args.length ? s.n : this.members.size;
      return s.n ? (e.op === "SUM" ? s.sum : s.best) : null;
    });
  }
}
export class View {
  private levels: JoinLevel[] = [];
  private offsets: number[] = [];
  private out: Output;
  private groups = new Map<string, Group>();
  private aggs: Expr[] = [];
  private global: boolean;
  names: string[];
  plan: Plan;
  constructor(plan: Plan, tables: Map<string, BaseRow[]>) {
    this.plan = plan;
    const q = plan.query;
    this.names = q.sources.map(([name]) => name);
    this.out = new Output(q.distinct);
    this.global = plan.aggregate && !q.groups.length;
    let width = 0;
    for (const t of plan.tables) {
      this.offsets.push(width);
      width += t.columns.length;
    }
    q.joins.forEach((join, k) => {
      const left = this.offsets[k + 1];
      const [leftKeys, rightKeys] = equalities(join.on, left);
      this.levels.push(
        new JoinLevel(
          join.on,
          join.outer,
          left,
          plan.tables[k + 1].columns.length,
          leftKeys,
          rightKeys,
        ),
      );
    });
    for (const [e] of q.select) this.number(e);
    if (q.having) this.number(q.having);
    /** A global aggregate keeps its single group even with no input rows. */
    if (this.global) this.groups.set("[]", new Group(this.aggs));
    /** Creating a view is the one full evaluation: every live row is a delta. */
    this.apply(
      new Map(
        this.names.map((n) => [
          n,
          { removed: [], added: tables.get(n) ?? [] } as Delta<BaseRow>,
        ]),
      ),
    );
    if (this.global) this.republish("[]", this.groups.get("[]")!);
  }
  /** Aggregate nodes get a slot so evaluation reads maintained state. */
  private number(e: Expr): void {
    for (const n of walk(e))
      if (aggregates.has(n.op)) {
        n.slot = this.aggs.length;
        this.aggs.push(n);
      }
  }
  private scan(i: number, rows: BaseRow[]): BaseRow[] {
    const filters = this.plan.filters[i];
    if (!filters.length) return rows;
    const blank = Array<Value>(this.offsets[i]);
    return rows.filter((r) =>
      filters.every((p) => holds(p, [...blank, ...r.values])),
    );
  }
  /** Push one committed batch of base changes through every stage. */
  apply(deltas: Map<string, Delta<BaseRow>>): void {
    if (!this.names.some((n) => deltas.has(n))) return;
    const scans = this.names.map((n, i) => {
      const d = deltas.get(n) ?? nothing;
      return { removed: this.scan(i, d.removed), added: this.scan(i, d.added) };
    });
    let delta: Delta<Tuple> = {
      removed: scans[0].removed.map(tuple0),
      added: scans[0].added.map(tuple0),
    };
    this.levels.forEach((level, k) => {
      delta = level.step(delta, scans[k + 1]);
    });
    const where = this.plan.query.where;
    if (where)
      delta = {
        removed: delta.removed.filter((t) => holds(where, t.values)),
        added: delta.added.filter((t) => holds(where, t.values)),
      };
    if (this.plan.aggregate) this.regroup(delta);
    else {
      /** Retractions run first: an updated row keeps its tuple identity. */
      for (const t of delta.removed) this.out.set(t.key, null, t.seqs);
      for (const t of delta.added)
        this.out.set(t.key, this.project(t.values), t.seqs);
    }
  }
  private project(values: Value[], slots?: Value[]): Value[] {
    return this.plan.query.select.map(([e]) => evaluate(e, values, [], slots));
  }
  private keyOf(t: Tuple): string {
    return JSON.stringify(
      this.plan.query.groups.map((e) => evaluate(e, t.values)),
    );
  }
  private regroup(delta: Delta<Tuple>): void {
    const touched = new Set<string>();
    for (const t of delta.removed) {
      const key = this.keyOf(t);
      const g = this.groups.get(key);
      if (!g) continue;
      g.remove(t);
      touched.add(key);
    }
    for (const t of delta.added) {
      const key = this.keyOf(t);
      let g = this.groups.get(key);
      if (!g) this.groups.set(key, (g = new Group(this.aggs)));
      g.add(t);
      touched.add(key);
    }
    for (const key of touched) {
      const g = this.groups.get(key)!;
      /** A grouped group disappears with its last member. */
      if (!g.members.size && !this.global) {
        this.groups.delete(key);
        this.out.set(key, null, []);
      } else this.republish(key, g);
    }
  }
  private republish(key: string, g: Group): void {
    const slots = g.values();
    const row = g.first ? g.first.values : [];
    const seqs = g.first ? g.first.seqs : [];
    const having = this.plan.query.having;
    this.out.set(
      key,
      having && !holds(having, row, [], slots) ? null : this.project(row, slots),
      seqs,
    );
  }
  read(): { columns: string[]; rows: Value[][] } {
    const q = this.plan.query;
    const rows = this.out.rows();
    rows.sort(
      (a, b) => compare(q.order, a.row, b.row) || earlier(a.seqs, b.seqs),
    );
    return {
      columns: q.select.map(([, a]) => a),
      rows: rows
        .slice(q.offset, q.limit === undefined ? undefined : q.offset + q.limit)
        .map((r) => [...r.row]),
    };
  }
}
function tuple0(r: BaseRow): Tuple {
  return { key: String(r.id), seqs: [r.seq], values: r.values };
}
/**
 * Equality conjuncts of an ON condition that compare the accumulated left input
 * with the joined table give both sides a hash key. Without any such conjunct
 * both sides share one bucket and the join falls back to a nested loop.
 */
function equalities(
  on: Expr,
  leftWidth: number,
): [Expr[] | null, Expr[] | null] {
  const left: Expr[] = [];
  const right: Expr[] = [];
  const parts: Expr[] = [];
  const split = (e: Expr): void => {
    if (e.op === "AND") e.args.forEach(split);
    else parts.push(e);
  };
  split(on);
  const side = (e: Expr): "left" | "right" | "both" | "none" => {
    let l = false;
    let r = false;
    for (const n of walk(e))
      if (n.op === "col") {
        if (n.index! < leftWidth) l = true;
        else r = true;
      }
    return l && r ? "both" : l ? "left" : r ? "right" : "none";
  };
  for (const p of parts) {
    if (p.op !== "=") continue;
    const [a, b] = p.args;
    const [x, y] = [side(a), side(b)];
    if (y === "right" && (x === "left" || x === "none")) {
      left.push(a);
      right.push(b);
    } else if (x === "right" && (y === "left" || y === "none")) {
      left.push(b);
      right.push(a);
    }
  }
  return left.length ? [left, right] : [null, null];
}
