/** Syntax tree, typed logical plan, and protocol validation. */
export type Value = number | string | boolean | null;
export type ScalarType = "int" | "text" | "bool";
export interface Column {
  name: string;
  type: ScalarType;
  nullable: boolean;
}
export interface Table {
  name: string;
  columns: Column[];
  rows: Value[][];
}
export interface InputQuery {
  sql: string;
  optimize: boolean;
}
export interface Expr {
  op: string;
  value?: Value | [string, string];
  args: Expr[];
  type?: ScalarType;
  index?: number;
  source?: number;
}
export interface JoinClause {
  on: Expr;
  left: boolean;
}
export interface OrderKey {
  alias: string;
  desc: boolean;
  nullsFirst: boolean;
  index?: number;
}
export interface Query {
  select: [Expr, string][];
  sources: [string, string][];
  joins: JoinClause[];
  where?: Expr;
  groups: Expr[];
  having?: Expr;
  order: OrderKey[];
  distinct: boolean;
  limit?: number;
  offset: number;
}
export interface Plan {
  query: Query;
  tables: Table[];
  filters: Expr[][];
  aggregate: boolean;
}
export class DomainError extends Error {}
export function requireThat(ok: unknown, code = "INVALID_INPUT"): asserts ok {
  if (!ok) throw new DomainError(code);
}
export const reserved = new Set(
  "SELECT DISTINCT AS FROM INNER JOIN LEFT OUTER ON WHERE GROUP BY HAVING ORDER ASC DESC NULLS FIRST LAST LIMIT OFFSET AND OR NOT IS NULL TRUE FALSE COALESCE COUNT SUM MIN MAX".split(
    " ",
  ),
);
export const aggregates = new Set(["COUNT", "SUM", "MIN", "MAX"]);
export function identifier(x: unknown): x is string {
  return (
    typeof x === "string" &&
    /^[a-z_][a-z0-9_]*$/.test(x) &&
    !reserved.has(x.toUpperCase())
  );
}
export function object(x: unknown): asserts x is Record<string, unknown> {
  requireThat(x !== null && typeof x === "object" && !Array.isArray(x));
}
export function fields(
  x: unknown,
  names: string[],
): asserts x is Record<string, unknown> {
  object(x);
  requireThat(
    Object.keys(x).length === names.length &&
      names.every((k) => Object.hasOwn(x, k)),
  );
}
function validateDatabase(data: unknown): Map<string, Table> {
  const database = new Map<string, Table>();
  requireThat(Array.isArray(data));
  for (const item of data as unknown[]) {
    fields(item, ["name", "columns", "rows"]);
    requireThat(identifier(item.name) && !database.has(item.name));
    requireThat(
      Array.isArray(item.columns) &&
        item.columns.length > 0 &&
        Array.isArray(item.rows),
    );
    const names = new Set<string>();
    const columns: Column[] = [];
    for (const c of item.columns as unknown[]) {
      fields(c, ["name", "type", "nullable"]);
      requireThat(identifier(c.name) && !names.has(c.name));
      requireThat(c.type === "int" || c.type === "text" || c.type === "bool");
      requireThat(typeof c.nullable === "boolean");
      names.add(c.name);
      columns.push({ name: c.name, type: c.type, nullable: c.nullable });
    }
    for (const row of item.rows as unknown[]) {
      requireThat(Array.isArray(row) && row.length === columns.length);
      row.forEach((v: unknown, i: number) => requireThat(checkValue(v, columns[i])));
    }
    database.set(item.name, {
      name: item.name,
      columns,
      rows: item.rows as Value[][],
    });
  }
  return database;
}
export function checkValue(v: unknown, column: Column): boolean {
  if (v === null) return column.nullable;
  if (column.type === "int") return typeof v === "number" && Number.isInteger(v);
  if (column.type === "text") return typeof v === "string";
  return typeof v === "boolean";
}
export interface CreateCommand {
  op: "create";
  view: string;
  sql: string;
  optimize: boolean;
}
export interface ReadCommand {
  op: "read";
  view: string;
}
export interface DropCommand {
  op: "drop";
  view: string;
}
export interface InsertChange {
  op: "insert";
  table: string;
  id: number;
  row: unknown[];
}
export interface UpdateChange {
  op: "update";
  table: string;
  id: number;
  row: unknown[];
}
export interface DeleteChange {
  op: "delete";
  table: string;
  id: number;
}
export type Change = InsertChange | UpdateChange | DeleteChange;
export interface ApplyCommand {
  op: "apply";
  changes: Change[];
}
export type Command = CreateCommand | ReadCommand | DropCommand | ApplyCommand;
const viewName = /^[a-z][a-z0-9-]{0,39}$/;
const tableName = /^[a-z_][a-z0-9_]*$/;
export class CommandError extends Error {}
function commandFields(x: unknown, names: string[]): asserts x is Record<string, unknown> {
  if (x === null || typeof x !== "object" || Array.isArray(x)) throw new CommandError("INVALID_COMMAND");
  if (Object.keys(x).length !== names.length || !names.every((k) => Object.hasOwn(x, k)))
    throw new CommandError("INVALID_COMMAND");
}
function need(ok: unknown): asserts ok {
  if (!ok) throw new CommandError("INVALID_COMMAND");
}
function parseChange(c: unknown): Change {
  need(c !== null && typeof c === "object" && !Array.isArray(c));
  const obj = c as Record<string, unknown>;
  if (obj.op === "delete") {
    commandFields(obj, ["op", "table", "id"]);
    need(typeof obj.table === "string" && tableName.test(obj.table));
    need(
      typeof obj.id === "number" &&
        Number.isInteger(obj.id) &&
        obj.id >= 1 &&
        obj.id <= 2147483647,
    );
    return { op: "delete", table: obj.table, id: obj.id };
  }
  need(obj.op === "insert" || obj.op === "update");
  commandFields(obj, ["op", "table", "id", "row"]);
  need(typeof obj.table === "string" && tableName.test(obj.table));
  need(
    typeof obj.id === "number" &&
      Number.isInteger(obj.id) &&
      obj.id >= 1 &&
      obj.id <= 2147483647,
  );
  need(Array.isArray(obj.row));
  return { op: obj.op, table: obj.table, id: obj.id, row: obj.row };
}
function parseCommand(c: unknown): Command {
  need(c !== null && typeof c === "object" && !Array.isArray(c));
  const obj = c as Record<string, unknown>;
  if (obj.op === "create") {
    commandFields(obj, ["op", "view", "sql", "optimize"]);
    need(typeof obj.view === "string" && viewName.test(obj.view));
    need(typeof obj.sql === "string");
    need(typeof obj.optimize === "boolean");
    return { op: "create", view: obj.view, sql: obj.sql, optimize: obj.optimize };
  }
  if (obj.op === "read") {
    commandFields(obj, ["op", "view"]);
    need(typeof obj.view === "string" && viewName.test(obj.view));
    return { op: "read", view: obj.view };
  }
  if (obj.op === "drop") {
    commandFields(obj, ["op", "view"]);
    need(typeof obj.view === "string" && viewName.test(obj.view));
    return { op: "drop", view: obj.view };
  }
  if (obj.op === "apply") {
    commandFields(obj, ["op", "changes"]);
    need(Array.isArray(obj.changes) && obj.changes.length >= 1 && obj.changes.length <= 200);
    return { op: "apply", changes: obj.changes.map(parseChange) };
  }
  throw new CommandError("INVALID_COMMAND");
}
export type Request =
  | { mode: "queries"; database: Map<string, Table>; queries: InputQuery[] }
  | { mode: "commands"; database: Map<string, Table>; commands: unknown[] };
export function validate(request: unknown): Request {
  object(request);
  requireThat(request.protocol_version === 1 && request.task === "query-null");
  const data = request.input;
  object(data);
  const hasQueries = Object.hasOwn(data, "queries");
  const hasCommands = Object.hasOwn(data, "commands");
  requireThat(hasQueries !== hasCommands);
  if (hasQueries) {
    fields(data, ["database", "queries"]);
    const database = validateDatabase(data.database);
    requireThat(Array.isArray(data.queries));
    const queries: InputQuery[] = [];
    for (const q of data.queries as unknown[]) {
      fields(q, ["sql", "optimize"]);
      requireThat(typeof q.sql === "string" && typeof q.optimize === "boolean");
      queries.push({ sql: q.sql, optimize: q.optimize });
    }
    return { mode: "queries", database, queries };
  }
  fields(data, ["database", "commands"]);
  const database = validateDatabase(data.database);
  requireThat(database.size <= 16);
  let totalRows = 0;
  for (const t of database.values()) totalRows += t.rows.length;
  requireThat(totalRows <= 20000);
  requireThat(Array.isArray(data.commands));
  requireThat((data.commands as unknown[]).length <= 2000);
  return { mode: "commands", database, commands: data.commands as unknown[] };
}
export function parseCommandSafe(c: unknown): Command {
  return parseCommand(c);
}
