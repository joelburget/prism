/** Base tables and incrementally maintained views. */
import {
  DomainError,
  CommandError,
  checkValue,
  parseCommandSafe,
  requireThat as need,
} from "./model.ts";
import type { Column, Table, Value, Query, Plan, Expr, Command, Change } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, evaluate } from "./engine.ts";

interface LiveRow {
  id: number;
  row: Value[];
  seq: number;
}

class BaseTable {
  columns: Column[];
  rows = new Map<number, LiveRow>();
  usedIds = new Set<number>();
  nextSeq = 0;
  constructor(t: Table) {
    this.columns = t.columns;
    t.rows.forEach((row, i) => {
      const id = i + 1;
      this.rows.set(id, { id, row, seq: this.nextSeq++ });
      this.usedIds.add(id);
    });
  }
  live(): LiveRow[] {
    return [...this.rows.values()].sort((a, b) => a.seq - b.seq);
  }
  snapshot() {
    return {
      rows: new Map(this.rows),
      usedIds: new Set(this.usedIds),
      nextSeq: this.nextSeq,
    };
  }
  restore(s: ReturnType<BaseTable["snapshot"]>): void {
    this.rows = s.rows;
    this.usedIds = s.usedIds;
    this.nextSeq = s.nextSeq;
  }
}

function validRow(row: unknown[], columns: Column[]): row is Value[] {
  if (row.length !== columns.length) return false;
  return row.every((v, i) => {
    const c = columns[i];
    if (v === null) return c.nullable;
    if (c.type === "int")
      return (
        typeof v === "number" &&
        Number.isInteger(v) &&
        v >= -1_000_000_000 &&
        v <= 1_000_000_000
      );
    if (c.type === "text") return typeof v === "string";
    return typeof v === "boolean";
  });
}

interface TupleEv {
  ids: number[];
  row: Value[];
  orderKey: number[];
  sign: 1 | -1;
}

function cmpKey(a: number[], b: number[]): number {
  for (let i = 0; i < a.length && i < b.length; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}
function minKey(keys: number[][]): number[] {
  let best = keys[0];
  for (const k of keys) if (cmpKey(k, best) < 0) best = k;
  return best;
}
function rowsEqual(a: Value[] | undefined, b: Value[] | undefined): boolean {
  if (a === undefined || b === undefined) return a === b;
  return JSON.stringify(a) === JSON.stringify(b);
}

interface GroupState {
  members: Map<string, { row: Value[]; orderKey: number[] }>;
  lastRow?: Value[];
  lastOrderKey?: number[];
}

class View {
  columns: string[];
  query: Query;
  sourceNames: string[];
  distinct: boolean;
  aggregate: boolean;
  filters: Expr[][];
  n: number;
  scans: Map<number, { id: number; seq: number; row: Value[] }>[];
  stage: Map<string, TupleEv>[];
  matches: Map<string, Map<string, TupleEv>>[];
  groups?: Map<string, GroupState>;
  distinctMembers?: Map<string, Map<string, number[]>>;
  output = new Map<string, { row: Value[]; orderKey: number[] }>();

  constructor(plan: Plan) {
    this.query = plan.query;
    this.columns = plan.query.select.map(([, a]) => a);
    this.sourceNames = plan.query.sources.map(([name]) => name);
    this.distinct = plan.query.distinct;
    this.aggregate = plan.aggregate;
    this.filters = plan.filters;
    this.n = plan.tables.length;
    this.scans = Array.from({ length: this.n }, () => new Map());
    this.stage = Array.from({ length: this.n }, () => new Map());
    this.matches = Array.from({ length: Math.max(this.n - 1, 0) }, () => new Map());
    if (this.aggregate) this.groups = new Map();
    if (this.distinct) this.distinctMembers = new Map();
    if (this.aggregate && plan.query.groups.length === 0) {
      const g: GroupState = { members: new Map() };
      this.groups!.set("[]", g);
      this.recomputeGroup("[]", g);
    }
  }

  private setContribution(
    key: string,
    row: Value[] | undefined,
    orderKey: number[],
    prevRow: Value[] | undefined,
  ): void {
    if (this.distinct) {
      const members = this.distinctMembers!;
      if (prevRow !== undefined) {
        const vkey = JSON.stringify(prevRow);
        const ms = members.get(vkey);
        if (ms) {
          ms.delete(key);
          if (ms.size === 0) {
            members.delete(vkey);
            this.output.delete(vkey);
          } else {
            this.output.set(vkey, { row: prevRow, orderKey: minKey([...ms.values()]) });
          }
        }
      }
      if (row !== undefined) {
        const vkey = JSON.stringify(row);
        let ms = members.get(vkey);
        if (!ms) {
          ms = new Map();
          members.set(vkey, ms);
        }
        ms.set(key, orderKey);
        this.output.set(vkey, { row, orderKey: minKey([...ms.values()]) });
      }
    } else {
      if (row !== undefined) this.output.set(key, { row, orderKey });
      else this.output.delete(key);
    }
  }

  private recomputeGroup(key: string, g: GroupState): void {
    const memberRows = [...g.members.values()].map((m) => m.row);
    const repRow = memberRows[0] ?? [];
    const passes =
      !this.query.having || evaluate(this.query.having, repRow, memberRows) === true;
    const newRow = passes
      ? this.query.select.map(([e]) => evaluate(e, repRow, memberRows))
      : undefined;
    const orderKey = g.members.size > 0 ? minKey([...g.members.values()].map((m) => m.orderKey)) : [];
    const changed =
      !rowsEqual(newRow, g.lastRow) ||
      (newRow !== undefined && JSON.stringify(orderKey) !== JSON.stringify(g.lastOrderKey));
    if (changed) this.setContribution(key, newRow, orderKey, g.lastRow);
    g.lastRow = newRow;
    g.lastOrderKey = newRow !== undefined ? orderKey : undefined;
  }

  private applyFinalEvent(ev: TupleEv): void {
    if (this.query.where && evaluate(this.query.where, ev.row) !== true) return;
    const lineage = ev.ids.join(",");
    if (this.aggregate) {
      const key = JSON.stringify(this.query.groups.map((e) => evaluate(e, ev.row)));
      let g = this.groups!.get(key);
      if (!g) {
        g = { members: new Map() };
        this.groups!.set(key, g);
      }
      if (ev.sign > 0) g.members.set(lineage, { row: ev.row, orderKey: ev.orderKey });
      else g.members.delete(lineage);
      if (g.members.size === 0 && this.query.groups.length > 0) {
        if (g.lastRow !== undefined) this.setContribution(key, undefined, [], g.lastRow);
        this.groups!.delete(key);
        return;
      }
      this.recomputeGroup(key, g);
    } else {
      if (ev.sign > 0) {
        const outRow = this.query.select.map(([e]) => evaluate(e, ev.row));
        this.setContribution(lineage, outRow, ev.orderKey, undefined);
      } else {
        const outRow = this.query.select.map(([e]) => evaluate(e, ev.row));
        this.setContribution(lineage, undefined, ev.orderKey, outRow);
      }
    }
  }

  private applyLeftEvent(step: number, ev: TupleEv): TupleEv[] {
    const lineage = ev.ids.join(",");
    if (ev.sign > 0) this.stage[step].set(lineage, ev);
    else this.stage[step].delete(lineage);
    const join = this.query.joins[step];
    const out: TupleEv[] = [];
    if (ev.sign > 0) {
      const m = new Map<string, TupleEv>();
      this.matches[step].set(lineage, m);
      for (const [id, r] of this.scans[step + 1]) {
        const combined = [...ev.row, ...r.row];
        if (evaluate(join.on, combined) === true) {
          const tup: TupleEv = {
            ids: [...ev.ids, id],
            row: combined,
            orderKey: [...ev.orderKey, r.seq],
            sign: 1,
          };
          m.set(String(id), tup);
          out.push(tup);
        }
      }
      if (m.size === 0 && join.left) {
        const width = this.widthOf(step + 1);
        const tup: TupleEv = {
          ids: [...ev.ids, -1],
          row: [...ev.row, ...Array<Value>(width).fill(null)],
          orderKey: [...ev.orderKey, -1],
          sign: 1,
        };
        m.set("NULL", tup);
        out.push(tup);
      }
    } else {
      const m = this.matches[step].get(lineage);
      if (m) {
        for (const tup of m.values()) out.push({ ...tup, sign: -1 });
        this.matches[step].delete(lineage);
      }
    }
    return out;
  }

  private applyRightEvent(
    step: number,
    id: number,
    seq: number,
    row: Value[] | undefined,
    sign: 1 | -1,
  ): TupleEv[] {
    const join = this.query.joins[step];
    const out: TupleEv[] = [];
    if (sign > 0) {
      this.scans[step + 1].set(id, { id, seq, row: row! });
      for (const [leftLineage, leftTup] of this.stage[step]) {
        const combined = [...leftTup.row, ...row!];
        if (evaluate(join.on, combined) === true) {
          let m = this.matches[step].get(leftLineage);
          if (!m) {
            m = new Map();
            this.matches[step].set(leftLineage, m);
          }
          if (join.left && m.size === 1 && m.has("NULL")) {
            const padded = m.get("NULL")!;
            m.delete("NULL");
            out.push({ ...padded, sign: -1 });
          }
          const tup: TupleEv = {
            ids: [...leftTup.ids, id],
            row: combined,
            orderKey: [...leftTup.orderKey, seq],
            sign: 1,
          };
          m.set(String(id), tup);
          out.push(tup);
        }
      }
    } else {
      this.scans[step + 1].delete(id);
      for (const [leftLineage, m] of this.matches[step]) {
        const key = String(id);
        if (m.has(key)) {
          const tup = m.get(key)!;
          m.delete(key);
          out.push({ ...tup, sign: -1 });
          if (m.size === 0 && join.left) {
            const leftTup = this.stage[step].get(leftLineage)!;
            const width = this.widthOf(step + 1);
            const padded: TupleEv = {
              ids: [...leftTup.ids, -1],
              row: [...leftTup.row, ...Array<Value>(width).fill(null)],
              orderKey: [...leftTup.orderKey, -1],
              sign: 1,
            };
            m.set("NULL", padded);
            out.push(padded);
          }
        }
      }
    }
    return out;
  }

  tableWidths: number[] = [];
  private widthOf(source: number): number {
    return this.tableWidths[source];
  }

  applySourceEvent(
    source: number,
    id: number,
    seq: number,
    row: Value[] | undefined,
    sign: 1 | -1,
  ): void {
    let events: TupleEv[];
    if (source === 0) {
      const lineage = String(id);
      const ev: TupleEv = { ids: [id], row: row!, orderKey: [seq], sign };
      if (sign > 0) this.stage[0].set(lineage, ev);
      else this.stage[0].delete(lineage);
      events = [ev];
      for (let step = 0; step < this.query.joins.length; step++) {
        const next: TupleEv[] = [];
        for (const e of events) next.push(...this.applyLeftEvent(step, e));
        events = next;
      }
    } else {
      events = this.applyRightEvent(source - 1, id, seq, row, sign);
      for (let step = source; step < this.query.joins.length; step++) {
        const next: TupleEv[] = [];
        for (const e of events) next.push(...this.applyLeftEvent(step, e));
        events = next;
      }
    }
    for (const e of events) this.applyFinalEvent(e);
  }

  passesFilter(source: number, row: Value[], offset: number): boolean {
    return this.filters[source].every((p) => evaluate(p, [...Array<Value>(offset), ...row]) === true);
  }
}

export class ViewDatabase {
  tables = new Map<string, BaseTable>();
  views = new Map<string, View>();
  revision = 0;
  constructor(database: Map<string, Table>) {
    for (const [name, t] of database) this.tables.set(name, new BaseTable(t));
  }

  handle(raw: unknown): { ok: true; result: unknown } | { ok: false; error: { code: string } } {
    try {
      const cmd = parseCommandSafe(raw);
      return { ok: true, result: this.exec(cmd) };
    } catch (e) {
      if (e instanceof CommandError || e instanceof DomainError)
        return { ok: false, error: { code: e.message } };
      throw e;
    }
  }

  private exec(cmd: Command): unknown {
    switch (cmd.op) {
      case "create":
        return this.create(cmd.view, cmd.sql, cmd.optimize);
      case "read":
        return this.read(cmd.view);
      case "drop":
        return this.drop(cmd.view);
      case "apply":
        return this.apply(cmd.changes);
    }
  }

  private create(name: string, sql: string, optimizeFlag: boolean): unknown {
    if (this.views.has(name)) throw new DomainError("VIEW_EXISTS");
    const database = new Map<string, Table>();
    for (const [tname, t] of this.tables)
      database.set(tname, { name: tname, columns: t.columns, rows: [] });
    const query = new Parser(sql).parse();
    let plan = bind(query, database);
    if (optimizeFlag) plan = optimize(plan);
    const view = new View(plan);
    let offset = 0;
    plan.tables.forEach((t) => {
      view.tableWidths.push(t.columns.length);
      offset += t.columns.length;
    });
    offset = 0;
    for (let k = 0; k < view.n; k++) {
      const tname = view.sourceNames[k];
      const table = this.tables.get(tname)!;
      const off = offset;
      for (const lr of table.live()) {
        if (view.passesFilter(k, lr.row, off)) view.applySourceEvent(k, lr.id, lr.seq, lr.row, 1);
      }
      offset += table.columns.length;
    }
    this.views.set(name, view);
    return { view: name, revision: this.revision };
  }

  private read(name: string): unknown {
    const view = this.views.get(name);
    if (!view) throw new DomainError("UNKNOWN_VIEW");
    const rows = [...view.output.values()];
    rows.sort((a, b) => {
      for (const o of view.query.order) {
        const i = o.index!;
        const av = a.row[i],
          bv = b.row[i];
        if (av === null || bv === null) {
          if (av === bv) continue;
          const c = av === null ? -1 : 1;
          const signed = o.nullsFirst ? c : -c;
          if (signed) return signed;
          continue;
        }
        const c = av < bv ? -1 : av > bv ? 1 : 0;
        if (c) return o.desc ? -c : c;
      }
      return cmpKey(a.orderKey, b.orderKey);
    });
    const limited = rows.slice(
      view.query.offset,
      view.query.limit === undefined ? undefined : view.query.offset + view.query.limit,
    );
    return {
      revision: this.revision,
      columns: view.columns,
      rows: limited.map((r) => r.row),
    };
  }

  private drop(name: string): unknown {
    if (!this.views.has(name)) throw new DomainError("UNKNOWN_VIEW");
    this.views.delete(name);
    return { dropped: name };
  }

  private apply(changes: Change[]): unknown {
    const touched = new Set<string>();
    for (const c of changes) if (this.tables.has(c.table)) touched.add(c.table);
    const snapshots = new Map<string, ReturnType<BaseTable["snapshot"]>>();
    for (const name of touched) snapshots.set(name, this.tables.get(name)!.snapshot());
    const events: { table: string; id: number; seq: number; oldRow?: Value[]; newRow?: Value[] }[] = [];
    try {
      for (const c of changes) {
        const table = this.tables.get(c.table);
        need(table, "UNKNOWN_TABLE");
        if (c.op === "delete") {
          need(table.rows.has(c.id), "UNKNOWN_ROW");
          const old = table.rows.get(c.id)!;
          table.rows.delete(c.id);
          events.push({ table: c.table, id: c.id, seq: old.seq, oldRow: old.row, newRow: undefined });
        } else if (c.op === "insert") {
          need(validRow(c.row, table.columns), "INVALID_ROW");
          need(!table.usedIds.has(c.id), "ROW_ID_USED");
          const seq = table.nextSeq++;
          table.rows.set(c.id, { id: c.id, row: c.row, seq });
          table.usedIds.add(c.id);
          events.push({ table: c.table, id: c.id, seq, oldRow: undefined, newRow: c.row });
        } else {
          need(validRow(c.row, table.columns), "INVALID_ROW");
          need(table.rows.has(c.id), "UNKNOWN_ROW");
          const old = table.rows.get(c.id)!;
          table.rows.set(c.id, { id: c.id, row: c.row, seq: old.seq });
          events.push({ table: c.table, id: c.id, seq: old.seq, oldRow: old.row, newRow: c.row });
        }
      }
    } catch (e) {
      for (const [name, snap] of snapshots) this.tables.get(name)!.restore(snap);
      throw e;
    }
    for (const view of this.views.values()) {
      let offset = 0;
      for (let k = 0; k < view.n; k++) {
        const tname = view.sourceNames[k];
        const width = view.tableWidths[k];
        for (const ev of events) {
          if (ev.table !== tname) continue;
          const oldPass = ev.oldRow !== undefined && view.passesFilter(k, ev.oldRow, offset);
          const newPass = ev.newRow !== undefined && view.passesFilter(k, ev.newRow, offset);
          if (oldPass) view.applySourceEvent(k, ev.id, ev.seq, ev.oldRow, -1);
          if (newPass) view.applySourceEvent(k, ev.id, ev.seq, ev.newRow, 1);
        }
        offset += width;
      }
    }
    this.revision += 1;
    return { revision: this.revision };
  }
}
