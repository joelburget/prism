import { readFileSync } from "node:fs";
import { DomainError, requireThat, validate, validateDatabase } from "./model.ts";
import type { Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";

type RecordRow = { row: Value[]; position: number };
type TableState = { table: Table; rows: Map<number, RecordRow>; used: Set<number>; nextPosition: number };
type View = { plan: ReturnType<typeof bind>; result: ReturnType<typeof execute>; sources: Set<string> };
const own = (x: unknown): x is Record<string, unknown> => x !== null && typeof x === "object" && !Array.isArray(x);
const exact = (x: unknown, keys: string[], code = "INVALID_COMMAND"): x is Record<string, unknown> => {
  if (!own(x) || Object.keys(x).length !== keys.length || !keys.every(k => Object.hasOwn(x, k))) throw new DomainError(code);
  return true;
};
const tableName = (x: unknown) => typeof x === "string" && /^[a-z_][a-z0-9_]*$/.test(x);
const viewName = (x: unknown) => typeof x === "string" && /^[a-z][a-z0-9-]{0,39}$/.test(x);
const rowValid = (t: Table, row: unknown): row is Value[] => Array.isArray(row) && row.length === t.columns.length && row.every((v, i) =>
  v === null ? t.columns[i].nullable : t.columns[i].type === "int" ? typeof v === "number" && Number.isInteger(v) && Math.abs(v) <= 1_000_000_000 : typeof v === (t.columns[i].type === "text" ? "string" : "boolean"));
function materialize(s: TableState): Value[][] { return [...s.rows.values()].sort((a, b) => a.position - b.position).map(r => r.row); }

function commandMode(request: unknown): unknown {
  // Validate schema before command dispatch, so malformed databases retain the old top-level error.
  const database = validateDatabase(request);
  if (!own(request) || !own(request.input)) throw new DomainError("INVALID_INPUT");
  const input = request.input;
  if (Object.keys(input).length !== 2 || !Object.hasOwn(input, "database") || !Object.hasOwn(input, "commands") || !Array.isArray(input.commands)) throw new DomainError("INVALID_INPUT");
  const states = new Map<string, TableState>();
  for (const [name, table] of database) {
    const rows = new Map<number, RecordRow>(); table.rows.forEach((row, i) => rows.set(i + 1, { row: [...row], position: i }));
    states.set(name, { table, rows, used: new Set(rows.keys()), nextPosition: rows.size });
  }
  const views = new Map<string, View>(); let revision = 0;
  const refresh = (changed: Set<string>) => { for (const view of views.values()) if ([...view.sources].some(s => changed.has(s))) view.result = execute(view.plan); };
  const reply = (fn: () => unknown) => { try { return { ok: true, result: fn() }; } catch (e) { if (e instanceof DomainError) return { ok: false, error: { code: e.message } }; throw e; } };
  const commands = input.commands as unknown[];
  return { results: commands.map(command => reply(() => {
    exact(command, ["op"]); // replaced below for each command, also rejects non-objects uniformly
    const op = command.op;
    if (op === "create") {
      exact(command, ["op", "view", "sql", "optimize"]); requireThat(viewName(command.view) && typeof command.sql === "string" && typeof command.optimize === "boolean", "INVALID_COMMAND");
      requireThat(!views.has(command.view), "VIEW_EXISTS");
      const plan = bind(new Parser(command.sql).parse(), database); const installed = command.optimize ? optimize(plan) : plan;
      views.set(command.view, { plan: installed, result: execute(installed), sources: new Set(installed.query.sources.map(([n]) => n)) });
      return { view: command.view, revision };
    }
    if (op === "read") { exact(command, ["op", "view"]); requireThat(viewName(command.view), "INVALID_COMMAND"); const v = views.get(command.view); requireThat(v, "UNKNOWN_VIEW"); return { revision, columns: [...v.result.columns], rows: v.result.rows.map(r => [...r]) }; }
    if (op === "drop") { exact(command, ["op", "view"]); requireThat(viewName(command.view), "INVALID_COMMAND"); requireThat(views.delete(command.view), "UNKNOWN_VIEW"); return { dropped: command.view }; }
    if (op !== "apply") throw new DomainError("INVALID_COMMAND");
    exact(command, ["op", "changes"]); requireThat(Array.isArray(command.changes) && command.changes.length > 0 && command.changes.length <= 200, "INVALID_COMMAND");
    // Shape validation is deliberately a separate pass: no domain failure may partially apply a batch.
    for (const c of command.changes) { if (!own(c) || !["insert", "update", "delete"].includes(c.op as string)) throw new DomainError("INVALID_COMMAND"); exact(c, c.op === "delete" ? ["op", "table", "id"] : ["op", "table", "id", "row"]); requireThat(tableName(c.table) && typeof c.id === "number" && Number.isInteger(c.id) && c.id >= 1 && c.id <= 2147483647 && (c.op === "delete" || Array.isArray(c.row)), "INVALID_COMMAND"); }
    const staged = new Map<string, TableState>();
    for (const [name, s] of states) staged.set(name, { table: s.table, rows: new Map([...s.rows].map(([id, r]) => [id, { row: [...r.row], position: r.position }])), used: new Set(s.used), nextPosition: s.nextPosition });
    const changed = new Set<string>();
    for (const c of command.changes as Record<string, unknown>[]) {
      const s = staged.get(c.table as string); requireThat(s, "UNKNOWN_TABLE"); const id = c.id as number;
      if (c.op === "insert") { requireThat(rowValid(s.table, c.row), "INVALID_ROW"); requireThat(!s.used.has(id), "ROW_ID_USED"); s.used.add(id); s.rows.set(id, { row: [...c.row as Value[]], position: s.nextPosition++ }); }
      else if (c.op === "update") { requireThat(rowValid(s.table, c.row), "INVALID_ROW"); const old = s.rows.get(id); requireThat(old, "UNKNOWN_ROW"); old.row = [...c.row as Value[]]; }
      else { requireThat(s.rows.delete(id), "UNKNOWN_ROW"); }
      changed.add(c.table as string);
    }
    for (const [name, s] of staged) { states.set(name, s); s.table.rows = materialize(s); }
    revision++; refresh(changed); return { revision };
  })) };
}

let response: unknown;
try {
  const request = JSON.parse(readFileSync(0, "utf8"), (_k, value, context?: { source?: string }) => { requireThat(typeof value !== "number" || !/[.eE]/.test(context?.source ?? ""), "INVALID_INPUT"); return value; }) as unknown;
  const isCommands = own(request) && own(request.input) && Object.hasOwn(request.input, "commands");
  if (isCommands) response = { ok: true, result: commandMode(request) };
  else { const [database, queries] = validate(request); response = { ok: true, result: { results: queries.map(q => { const p = bind(new Parser(q.sql).parse(), database); return execute(q.optimize ? optimize(p) : p); }) } }; }
} catch (e) {
  if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
  response = { ok: false, error: { code: e instanceof DomainError ? e.message : "INVALID_INPUT" } };
}
process.stdout.write(JSON.stringify(response) + "\n");
