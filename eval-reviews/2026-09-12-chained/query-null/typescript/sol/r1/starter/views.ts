/** Incremental materialized views. Join rows carry base-row provenance so a
 * batch can retract and replace only affected join branches and groups. */
import type { Expr, Plan, Table, Value } from "./model.ts";
import { evaluate } from "./engine.ts";

export interface TableState {
  table: Table;
  order: number[];
  positions: Map<number, number>;
  rows: Map<number, Value[]>;
  used: Set<number>;
}
interface RecordRow { values: Value[]; ids: (number | null)[] }
interface CachedGroup { records: Map<string, RecordRow>; projected?: Value[]; order: number[] }
const keyOf = (ids: (number | null)[]) => JSON.stringify(ids);
const same = (a?: RecordRow, b?: RecordRow) =>
  !!a && !!b && JSON.stringify(a.values) === JSON.stringify(b.values);

export class MaterializedView {
  readonly plan: Plan;
  private states: Map<string, TableState>;
  private stages: Map<string, RecordRow>[] = [];
  private sourceRows: Map<number, RecordRow>[] = [];
  private qualified = new Map<string, RecordRow>();
  private contributions = new Map<string, RecordRow>();
  private groups = new Map<string, CachedGroup>();
  readonly dependencies: Set<string>;

  constructor(plan: Plan, states: Map<string, TableState>) {
    this.plan = plan;
    this.states = states;
    this.dependencies = new Set(plan.tables.map((t) => t.name));
    this.buildInitial();
  }

  private offset(source: number): number {
    return this.plan.tables.slice(0, source).reduce((n, t) => n + t.columns.length, 0);
  }
  private scanOne(source: number, id: number, row: Value[]): RecordRow | undefined {
    const padding = Array<Value>(this.offset(source)).fill(null);
    return this.plan.filters[source].every((p) => evaluate(p, [...padding, ...row]) === true)
      ? { values: row, ids: [id] } : undefined;
  }
  private scan(source: number): RecordRow[] {
    const table = this.plan.tables[source], state = this.states.get(table.name)!;
    return state.order.flatMap((id) => {
      const row = state.rows.get(id);
      const record = row ? this.scanOne(source, id, row) : undefined;
      return record ? [record] : [];
    });
  }
  private currentScan(source: number): RecordRow[] {
    const state = this.states.get(this.plan.tables[source].name)!;
    const cached = this.sourceRows[source];
    return state.order.flatMap((id) => {
      const record = cached.get(id);
      return record ? [record] : [];
    });
  }
  private joined(left: RecordRow, rights: RecordRow[], source: number): RecordRow[] {
    const join = this.plan.query.joins[source - 1];
    const matches = rights.flatMap((right) => {
      const values = [...left.values, ...right.values];
      return evaluate(join.on, values) === true
        ? [{ values, ids: [...left.ids, right.ids[0]] }] : [];
    });
    return matches.length || join.kind === "inner" ? matches : [{
      values: [...left.values, ...Array<Value>(this.plan.tables[source].columns.length).fill(null)],
      ids: [...left.ids, null],
    }];
  }
  private encounter(a: RecordRow, b: RecordRow): number {
    for (let i = 0; i < a.ids.length; i++) {
      const state = this.states.get(this.plan.tables[i].name)!;
      const av = a.ids[i] === null ? -1 : state.positions.get(a.ids[i]!)!;
      const bv = b.ids[i] === null ? -1 : state.positions.get(b.ids[i]!)!;
      if (av !== bv) return av - bv;
    }
    return 0;
  }
  private ordered(map: Map<string, RecordRow>): RecordRow[] {
    return [...map.values()].sort((a, b) => this.encounter(a, b));
  }
  private buildInitial(): void {
    this.sourceRows = this.plan.tables.map((_, source) =>
      new Map(this.scan(source).map((r) => [r.ids[0]!, r])));
    let rows = this.currentScan(0);
    this.stages.push(new Map(rows.map((r) => [keyOf(r.ids), r])));
    for (let source = 1; source < this.plan.tables.length; source++) {
      const rights = this.currentScan(source);
      rows = rows.flatMap((left) => this.joined(left, rights, source));
      this.stages.push(new Map(rows.map((r) => [keyOf(r.ids), r])));
    }
    for (const r of rows) if (!this.plan.query.where || evaluate(this.plan.query.where, r.values) === true)
      this.qualified.set(keyOf(r.ids), r);
    this.initializeOutput();
  }
  private groupKey(r: RecordRow): string {
    return JSON.stringify(this.plan.query.groups.map((e) => evaluate(e, r.values)));
  }
  private recomputeGroup(key: string): void {
    let group = this.groups.get(key);
    if (!group) {
      if (key !== "[]" || this.plan.query.groups.length) return;
      group = { records: new Map(), order: [] };
      this.groups.set(key, group);
    }
    if (!group.records.size && this.plan.query.groups.length) {
      this.groups.delete(key);
      return;
    }
    const records = this.ordered(group.records), rows = records.map((r) => r.values);
    const representative = rows[0] ?? [];
    group.order = records[0]?.ids.map((x) => x ?? -1) ?? [];
    group.projected = !this.plan.query.having || evaluate(this.plan.query.having, representative, rows) === true
      ? this.plan.query.select.map(([e]) => evaluate(e, representative, rows)) : undefined;
  }
  private initializeOutput(): void {
    if (!this.plan.aggregate) {
      for (const [key, r] of this.qualified)
        this.contributions.set(key, { ids: r.ids, values: this.plan.query.select.map(([e]) => evaluate(e, r.values)) });
      return;
    }
    for (const [key, r] of this.qualified) {
      const gk = this.groupKey(r), group = this.groups.get(gk) ?? { records: new Map(), order: [] };
      group.records.set(key, r); this.groups.set(gk, group);
    }
    if (!this.plan.query.groups.length && !this.groups.has("[]"))
      this.groups.set("[]", { records: new Map(), order: [] });
    for (const key of this.groups.keys()) this.recomputeGroup(key);
  }

  apply(changed: Map<string, Set<number>>): void {
    if (![...this.dependencies].some((name) => changed.has(name))) return;
    for (let source = 0; source < this.plan.tables.length; source++) {
      const ids = changed.get(this.plan.tables[source].name);
      if (!ids) continue;
      const state = this.states.get(this.plan.tables[source].name)!;
      for (const id of ids) {
        this.sourceRows[source].delete(id);
        const row = state.rows.get(id), record = row && this.scanOne(source, id, row);
        if (record) this.sourceRows[source].set(id, record);
      }
    }
    const scans = this.plan.tables.map((_, i) => this.currentScan(i));
    const oldFinal = this.stages.at(-1)!;
    let propagated = new Set<string>();
    if (changed.has(this.plan.tables[0].name)) {
      const old = this.stages[0], next = new Map(scans[0].map((r) => [keyOf(r.ids), r]));
      for (const key of new Set([...old.keys(), ...next.keys()])) if (!same(old.get(key), next.get(key))) propagated.add(key);
      this.stages[0] = next;
    }
    for (let source = 1; source < this.plan.tables.length; source++) {
      const old = this.stages[source], left = this.stages[source - 1];
      const affected = new Set(propagated);
      const touched = changed.get(this.plan.tables[source].name);
      if (touched) {
        for (const r of old.values()) if (r.ids[source] !== null && touched.has(r.ids[source]!))
          affected.add(keyOf(r.ids.slice(0, source)));
        const changedRights = scans[source].filter((r) => touched.has(r.ids[0]!));
        for (const l of left.values()) if (changedRights.some((r) => evaluate(this.plan.query.joins[source - 1].on, [...l.values, ...r.values]) === true))
          affected.add(keyOf(l.ids));
      }
      if (!affected.size) { propagated = new Set(); continue; }
      const next = new Map<string, RecordRow>();
      for (const [key, r] of old) if (!affected.has(keyOf(r.ids.slice(0, source)))) next.set(key, r);
      for (const l of left.values()) if (affected.has(keyOf(l.ids)))
        for (const r of this.joined(l, scans[source], source)) next.set(keyOf(r.ids), r);
      propagated = new Set();
      for (const key of new Set([...old.keys(), ...next.keys()])) if (!same(old.get(key), next.get(key))) propagated.add(key);
      this.stages[source] = next;
    }
    const newFinal = this.stages.at(-1)!;
    const delta = new Set<string>();
    for (const key of new Set([...oldFinal.keys(), ...newFinal.keys()])) if (!same(oldFinal.get(key), newFinal.get(key))) delta.add(key);
    const removed: RecordRow[] = [], added: RecordRow[] = [];
    for (const key of delta) {
      const old = this.qualified.get(key), candidate = newFinal.get(key);
      if (old) { removed.push(old); this.qualified.delete(key); }
      if (candidate && (!this.plan.query.where || evaluate(this.plan.query.where, candidate.values) === true)) {
        this.qualified.set(key, candidate); added.push(candidate);
      }
    }
    this.updateOutput(removed, added);
  }
  private updateOutput(removed: RecordRow[], added: RecordRow[]): void {
    if (!this.plan.aggregate) {
      for (const r of removed) this.contributions.delete(keyOf(r.ids));
      for (const r of added) this.contributions.set(keyOf(r.ids), {
        ids: r.ids, values: this.plan.query.select.map(([e]) => evaluate(e, r.values)),
      });
      return;
    }
    const touched = new Set<string>();
    for (const r of removed) {
      const gk = this.groupKey(r); touched.add(gk); this.groups.get(gk)?.records.delete(keyOf(r.ids));
    }
    for (const r of added) {
      const gk = this.groupKey(r), group = this.groups.get(gk) ?? { records: new Map(), order: [] };
      group.records.set(keyOf(r.ids), r); this.groups.set(gk, group); touched.add(gk);
    }
    if (!this.plan.query.groups.length) touched.add("[]");
    for (const key of touched) this.recomputeGroup(key);
  }

  read(): { columns: string[]; rows: Value[][] } {
    let records: RecordRow[];
    if (!this.plan.aggregate) records = this.ordered(this.contributions);
    else records = [...this.groups.values()].filter((g) => g.projected).map((g) => ({
      values: g.projected!, ids: g.records.size ? this.ordered(g.records)[0].ids : [],
    })).sort((a, b) => this.encounter(a, b));
    let rows = records.map((r) => r.values);
    if (this.plan.query.distinct) {
      const seen = new Set<string>();
      rows = rows.filter((r) => { const k = JSON.stringify(r); if (seen.has(k)) return false; seen.add(k); return true; });
    }
    rows.sort((a, b) => {
      for (const [raw, desc, explicit] of this.plan.query.order) {
        const i = raw as number;
        if (a[i] === null || b[i] === null) {
          if (a[i] === b[i]) continue;
          const first = explicit ?? desc;
          return a[i] === null ? (first ? -1 : 1) : first ? 1 : -1;
        }
        const c = a[i] < b[i] ? -1 : a[i] > b[i] ? 1 : 0;
        if (c) return desc ? -c : c;
      }
      return 0;
    });
    const q = this.plan.query;
    return { columns: q.select.map(([, a]) => a), rows: rows.slice(q.offset, q.limit === undefined ? undefined : q.offset + q.limit).map((r) => [...r]) };
  }
}
