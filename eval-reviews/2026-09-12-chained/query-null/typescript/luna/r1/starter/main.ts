import { readFileSync } from "node:fs";
import { DomainError, validate, requireThat, cloneDatabase } from "./model.ts";
import type { Table, Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";

type Result = { columns: string[]; rows: Value[][] };
type View = { sql: string; optimize: boolean; tables: Set<string>; result: Result };

function run(sql: string, useOptimizer: boolean, database: Map<string, Table>): { result: Result; tables: Set<string> } {
  const query = new Parser(sql).parse();
  const plan = bind(query, database);
  const finalPlan = useOptimizer ? optimize(plan) : plan;
  return { result: execute(finalPlan), tables: new Set(query.sources.map(([name]) => name)) };
}
function commandShape(x: unknown, names: string[]): asserts x is Record<string, unknown> {
  requireThat(x !== null && typeof x === "object" && !Array.isArray(x), "INVALID_COMMAND");
  requireThat(Object.keys(x as object).length === names.length && names.every(n => Object.hasOwn(x as object, n)), "INVALID_COMMAND");
}
function tableName(x: unknown): x is string { return typeof x === "string" && /^[a-z_][a-z0-9_]*$/.test(x); }
function validId(x: unknown): x is number { return typeof x === "number" && Number.isInteger(x) && Number.isSafeInteger(x) && x >= 1 && x <= 2147483647; }
function validateRow(table: Table, row: unknown): asserts row is Value[] {
  requireThat(Array.isArray(row) && row.length === table.columns.length, "INVALID_ROW");
  (row as unknown[]).forEach((v, i) => requireThat(
    v === null ? table.columns[i].nullable : table.columns[i].type === "int"
      ? typeof v === "number" && Number.isInteger(v) && Number.isSafeInteger(v)
      : typeof v === (table.columns[i].type === "text" ? "string" : "boolean"), "INVALID_ROW"));
}
function processCommands(initial: Map<string, Table>, commands: unknown[]): unknown[] {
  let database = initial;
  let revision = 0;
  const views = new Map<string, View>();
  const replies: unknown[] = [];
  for (const raw of commands) {
    try {
      requireThat(raw !== null && typeof raw === "object" && !Array.isArray(raw) && Object.hasOwn(raw, "op"), "INVALID_COMMAND");
      const record = raw as Record<string, unknown>;
      const op = record.op;
      if (op === "create") {
        commandShape(raw, ["op", "view", "sql", "optimize"]);
        requireThat(typeof raw.view === "string" && /^[a-z][a-z0-9-]{0,39}$/.test(raw.view), "INVALID_COMMAND");
        requireThat(typeof raw.sql === "string" && typeof raw.optimize === "boolean", "INVALID_COMMAND");
        requireThat(!views.has(raw.view), "VIEW_EXISTS");
        const built = run(raw.sql, raw.optimize, database);
        views.set(raw.view, { sql: raw.sql, optimize: raw.optimize, tables: built.tables, result: built.result });
        replies.push({ ok: true, result: { view: raw.view, revision } });
      } else if (op === "read") {
        commandShape(raw, ["op", "view"]);
        requireThat(typeof raw.view === "string", "INVALID_COMMAND");
        const view = views.get(raw.view);
        requireThat(view, "UNKNOWN_VIEW");
        replies.push({ ok: true, result: { revision, columns: [...view.result.columns], rows: view.result.rows.map(r => [...r]) } });
      } else if (op === "drop") {
        commandShape(raw, ["op", "view"]);
        requireThat(typeof raw.view === "string", "INVALID_COMMAND");
        requireThat(views.delete(raw.view), "UNKNOWN_VIEW");
        replies.push({ ok: true, result: { dropped: raw.view } });
      } else if (op === "apply") {
        commandShape(raw, ["op", "changes"]);
        requireThat(Array.isArray(raw.changes) && raw.changes.length > 0 && raw.changes.length <= 200, "INVALID_COMMAND");
        const candidate = cloneDatabase(database);
        const changed = new Set<string>();
        for (const c of raw.changes) {
          requireThat(c !== null && typeof c === "object" && !Array.isArray(c), "INVALID_COMMAND");
          const cop = (c as Record<string, unknown>).op;
          const fields = cop === "insert" ? ["op", "table", "id", "row"] : cop === "update" ? ["op", "table", "id", "row"] : cop === "delete" ? ["op", "table", "id"] : [];
          commandShape(c, fields);
          requireThat(tableName(c.table) && validId(c.id), "INVALID_COMMAND");
          const table = candidate.get(c.table);
          requireThat(table, "UNKNOWN_TABLE");
          changed.add(c.table);
          if (cop === "insert") {
            validateRow(table, c.row); requireThat(!table.usedIds.has(c.id), "ROW_ID_USED");
            table.rows.push(c.row); table.rowIds.push(c.id); table.usedIds.add(c.id);
          } else {
            const pos = table.rowIds.indexOf(c.id); requireThat(pos >= 0, "UNKNOWN_ROW");
            if (cop === "update") { validateRow(table, c.row); table.rows[pos] = c.row; }
            else { table.rows.splice(pos, 1); table.rowIds.splice(pos, 1); }
          }
        }
        for (const view of views.values()) if ([...changed].some(t => view.tables.has(t))) {
          const built = run(view.sql, view.optimize, candidate); view.tables = built.tables; view.result = built.result;
        }
        database = candidate; revision++;
        replies.push({ ok: true, result: { revision } });
      } else requireThat(false, "INVALID_COMMAND");
    } catch (e) {
      if (!(e instanceof DomainError)) throw e;
      replies.push({ ok: false, error: { code: e.message } });
    }
  }
  return replies;
}

let response: unknown;
try {
  const parsed = JSON.parse(readFileSync(0, "utf8"), (_key: string, value: unknown, context?: { source?: string }) => {
    requireThat(typeof value !== "number" || !/[.eE]/.test(context?.source ?? ""), "INVALID_INPUT"); return value;
  }) as unknown;
  const request = validate(parsed);
  if ("queries" in request) {
    const results = request.queries.map(q => run(q.sql, q.optimize, request.database).result);
    response = { ok: true, result: { results } };
  } else response = { ok: true, result: { results: processCommands(request.database, request.commands) } };
} catch (e) {
  if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
  response = { ok: false, error: { code: e instanceof DomainError ? e.message : "INVALID_INPUT" } };
}
process.stdout.write(JSON.stringify(response) + "\n");
