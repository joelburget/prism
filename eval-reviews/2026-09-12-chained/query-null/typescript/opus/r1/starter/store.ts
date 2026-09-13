/**
 * Base tables, the command protocol, and atomic batches.
 *
 * Rows live here with a private identifier and an encounter position; the views
 * own the derived state and are told only what changed. A batch is staged in a
 * private overlay, so a failing change leaves base rows, reserved identifiers,
 * view state and the revision untouched.
 */
import {
  DomainError,
  fields,
  requireThat as need,
  tableName,
  viewName,
} from "./model.ts";
import type { Column, Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize } from "./engine.ts";
import { View } from "./views.ts";
import type { BaseRow, Delta } from "./views.ts";

const bound = 1000000000;
const maxId = 2147483647;
interface Base {
  table: Table;
  rows: Map<number, BaseRow>;
  used: Set<number>;
  seq: number;
}
/** The staged effect of one batch on one table. */
interface Staged {
  rows: Map<number, BaseRow | null>;
  used: Set<number>;
  seq: number;
}
interface Change {
  op: string;
  table: string;
  id: number;
  row: unknown[];
}
function checkRow(columns: Column[], row: unknown[]): Value[] {
  need(row.length === columns.length, "INVALID_ROW");
  return row.map((v, i) => {
    const c = columns[i];
    if (v === null) {
      need(c.nullable, "INVALID_ROW");
      return null;
    }
    if (c.type === "int")
      need(
        typeof v === "number" && Number.isInteger(v) && Math.abs(v) <= bound,
        "INVALID_ROW",
      );
    else need(typeof v === (c.type === "text" ? "string" : "boolean"), "INVALID_ROW");
    return v as Value;
  });
}
/** Shapes and scalar constraints of every change are checked before any apply. */
function readChange(c: unknown): Change {
  need(c !== null && typeof c === "object" && !Array.isArray(c), "INVALID_COMMAND");
  const r = c as Record<string, unknown>;
  const op = r.op;
  need(op === "insert" || op === "update" || op === "delete", "INVALID_COMMAND");
  fields(
    r,
    op === "delete" ? ["op", "table", "id"] : ["op", "table", "id", "row"],
    "INVALID_COMMAND",
  );
  need(typeof r.table === "string" && tableName.test(r.table), "INVALID_COMMAND");
  need(
    typeof r.id === "number" &&
      Number.isInteger(r.id) &&
      r.id >= 1 &&
      r.id <= maxId,
    "INVALID_COMMAND",
  );
  if (op !== "delete") need(Array.isArray(r.row), "INVALID_COMMAND");
  return { op, table: r.table, id: r.id, row: (r.row ?? []) as unknown[] };
}
function reply(body: () => unknown): unknown {
  try {
    return { ok: true, result: body() };
  } catch (e) {
    if (!(e instanceof DomainError)) throw e;
    return { ok: false, error: { code: e.message } };
  }
}
export class Store {
  bases = new Map<string, Base>();
  views = new Map<string, View>();
  revision = 0;
  schema: Map<string, Table>;
  constructor(schema: Map<string, Table>) {
    this.schema = schema;
    for (const [name, table] of schema) {
      const rows = new Map<number, BaseRow>();
      const used = new Set<number>();
      /** Initial rows own identifiers 1..N per table, in input order. */
      table.rows.forEach((values, i) => {
        rows.set(i + 1, { id: i + 1, seq: i, values });
        used.add(i + 1);
      });
      this.bases.set(name, { table, rows, used, seq: table.rows.length });
    }
  }
  run(commands: unknown[]): unknown[] {
    return commands.map((c) => reply(() => this.command(c)));
  }
  private command(c: unknown): unknown {
    need(c !== null && typeof c === "object" && !Array.isArray(c), "INVALID_COMMAND");
    const r = c as Record<string, unknown>;
    if (r.op === "create") return this.create(r);
    if (r.op === "read" || r.op === "drop") {
      fields(r, ["op", "view"], "INVALID_COMMAND");
      const name = this.viewOf(r);
      if (r.op === "drop") {
        need(this.views.delete(name), "UNKNOWN_VIEW");
        return { dropped: name };
      }
      const view = this.views.get(name);
      need(view, "UNKNOWN_VIEW");
      return { revision: this.revision, ...view.read() };
    }
    if (r.op === "apply") return this.apply(r);
    throw new DomainError("INVALID_COMMAND");
  }
  private viewOf(r: Record<string, unknown>): string {
    need(typeof r.view === "string" && viewName.test(r.view), "INVALID_COMMAND");
    return r.view as string;
  }
  private create(r: Record<string, unknown>): unknown {
    fields(r, ["op", "view", "sql", "optimize"], "INVALID_COMMAND");
    const name = this.viewOf(r);
    need(typeof r.sql === "string", "INVALID_COMMAND");
    need(typeof r.optimize === "boolean", "INVALID_COMMAND");
    need(!this.views.has(name), "VIEW_EXISTS");
    const plan = bind(new Parser(r.sql as string).parse(), this.schema);
    const live = new Map<string, BaseRow[]>();
    for (const [table] of plan.query.sources)
      live.set(table, [...this.bases.get(table)!.rows.values()]);
    this.views.set(name, new View(r.optimize ? optimize(plan) : plan, live));
    return { view: name, revision: this.revision };
  }
  private apply(r: Record<string, unknown>): unknown {
    fields(r, ["op", "changes"], "INVALID_COMMAND");
    need(Array.isArray(r.changes), "INVALID_COMMAND");
    const changes = (r.changes as unknown[]).map(readChange);
    need(changes.length > 0 && changes.length <= 200, "INVALID_COMMAND");
    const staged = new Map<string, Staged>();
    for (const c of changes) {
      const base = this.bases.get(c.table);
      need(base, "UNKNOWN_TABLE");
      let s = staged.get(c.table);
      if (!s)
        staged.set(
          c.table,
          (s = { rows: new Map(), used: new Set(), seq: base.seq }),
        );
      const current = s.rows.has(c.id)
        ? s.rows.get(c.id)!
        : (base.rows.get(c.id) ?? null);
      if (c.op === "delete") {
        need(current, "UNKNOWN_ROW");
        s.rows.set(c.id, null);
        continue;
      }
      const values = checkRow(base.table.columns, c.row);
      if (c.op === "insert") {
        need(!base.used.has(c.id) && !s.used.has(c.id), "ROW_ID_USED");
        s.used.add(c.id);
        /** An insertion appends; its position is never reused. */
        s.rows.set(c.id, { id: c.id, seq: s.seq++, values });
      } else {
        need(current, "UNKNOWN_ROW");
        /** An update keeps the row's encounter position. */
        s.rows.set(c.id, { id: c.id, seq: current.seq, values });
      }
    }
    /** Every change succeeded: commit rows, identifiers and views together. */
    const deltas = new Map<string, Delta<BaseRow>>();
    for (const [name, s] of staged) {
      const base = this.bases.get(name)!;
      const delta: Delta<BaseRow> = { removed: [], added: [] };
      for (const [id, row] of s.rows) {
        const before = base.rows.get(id) ?? null;
        if (before && row && row.values.every((v, i) => v === before.values[i]))
          continue;
        if (before) delta.removed.push(before);
        if (row) delta.added.push(row);
        if (row) base.rows.set(id, row);
        else base.rows.delete(id);
      }
      for (const id of s.used) base.used.add(id);
      base.seq = s.seq;
      deltas.set(name, delta);
    }
    this.revision++;
    for (const view of this.views.values()) view.apply(deltas);
    return { revision: this.revision };
  }
}
