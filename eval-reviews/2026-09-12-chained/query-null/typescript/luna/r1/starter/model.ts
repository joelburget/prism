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
  rowIds: number[];
  usedIds: Set<number>;
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
export interface Query {
  select: [Expr, string][];
  sources: [string, string][];
  joins: { on: Expr; left: boolean }[];
  where?: Expr;
  groups: Expr[];
  having?: Expr;
  order: [string | number, boolean, boolean | undefined][];
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
function object(x: unknown): asserts x is Record<string, unknown> {
  requireThat(x !== null && typeof x === "object" && !Array.isArray(x));
}
function fields(
  x: unknown,
  names: string[],
): asserts x is Record<string, unknown> {
  object(x);
  requireThat(
    Object.keys(x).length === names.length &&
      names.every((k) => Object.hasOwn(x, k)),
  );
}
function validateDatabase(items: unknown): Map<string, Table> {
  requireThat(Array.isArray(items));
  const database = new Map<string, Table>();
  for (const item of items as unknown[]) {
    fields(item, ["name", "columns", "rows"]);
    requireThat(identifier(item.name) && !database.has(item.name));
    requireThat(Array.isArray(item.columns) && item.columns.length > 0 && Array.isArray(item.rows));
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
    const rows: Value[][] = [];
    for (const row of item.rows as unknown[]) {
      requireThat(Array.isArray(row) && row.length === columns.length);
      row.forEach((v: unknown, i: number) => requireThat(
        v === null ? columns[i].nullable : columns[i].type === "int"
          ? typeof v === "number" && Number.isInteger(v) && Number.isSafeInteger(v)
          : typeof v === (columns[i].type === "text" ? "string" : "boolean"),
      ));
      rows.push(row as Value[]);
    }
    const rowIds = rows.map((_, i) => i + 1);
    database.set(item.name as string, { name: item.name as string, columns, rows, rowIds, usedIds: new Set(rowIds) });
  }
  return database;
}

export type Request =
  | { database: Map<string, Table>; queries: InputQuery[] }
  | { database: Map<string, Table>; commands: unknown[] };

export function validate(request: unknown): Request {
  object(request);
  requireThat(request.protocol_version === 1 && request.task === "query-null");
  const data = request.input;
  requireThat(data !== null && typeof data === "object");
  const input = data as Record<string, unknown>;
  const database = validateDatabase(input.database);
  requireThat(Array.isArray(input.queries) || Array.isArray(input.commands));
  requireThat(!(Array.isArray(input.queries) && Array.isArray(input.commands)));
  if (Array.isArray(input.queries)) {
    fields(input, ["database", "queries"]);
    const queries: InputQuery[] = [];
    for (const q of input.queries as unknown[]) {
      fields(q, ["sql", "optimize"]);
      requireThat(typeof q.sql === "string" && typeof q.optimize === "boolean");
      queries.push({ sql: q.sql, optimize: q.optimize });
    }
    return { database, queries };
  }
  fields(input, ["database", "commands"]);
  requireThat((input.commands as unknown[]).length <= 2000);
  return { database, commands: input.commands as unknown[] };
}

export function cloneDatabase(source: Map<string, Table>): Map<string, Table> {
  return new Map([...source].map(([name, t]) => [name, {
    ...t, columns: t.columns.map(c => ({ ...c })), rows: t.rows.map(r => [...r]),
    rowIds: [...t.rowIds], usedIds: new Set(t.usedIds),
  }]));
}
