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
/** A validated request: the old query list or the new command list. */
export interface Request {
  database: Map<string, Table>;
  queries?: InputQuery[];
  commands?: unknown[];
}
/**
 * Bound expression node. `type` is undefined only for an untyped NULL literal
 * (or an expression whose only inputs are untyped NULLs); such a node takes the
 * type demanded by its context. `nullable` is a conservative static bound: false
 * means the expression can never evaluate to NULL in the context it was bound
 * in (declared nullability, outer-join padding, and aggregate emptiness are all
 * accounted for); true means it might.
 */
export interface Expr {
  op: string;
  value?: Value | [string, string];
  args: Expr[];
  type?: ScalarType;
  nullable?: boolean;
  index?: number;
  source?: number;
}
/** ORDER BY item: output alias (index after binding), DESC flag, NULLS FIRST flag. */
export type OrderItem = [string | number, boolean, boolean];
export interface Query {
  select: [Expr, string][];
  sources: [string, string][];
  joins: Expr[];
  /** outer[i] is true when joins[i] is a LEFT OUTER JOIN (pads sources[i + 1]). */
  outer: boolean[];
  where?: Expr;
  groups: Expr[];
  having?: Expr;
  order: OrderItem[];
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
export function typed(v: unknown, type: ScalarType): boolean {
  if (type === "int") return typeof v === "number" && Number.isInteger(v);
  return typeof v === (type === "text" ? "string" : "boolean");
}
export function validate(request: unknown): Request {
  object(request);
  requireThat(request.protocol_version === 1 && request.task === "query-null");
  const data = request.input;
  object(data);
  const commandMode = Object.hasOwn(data, "commands");
  fields(data, ["database", commandMode ? "commands" : "queries"]);
  requireThat(Array.isArray(data.database));
  requireThat(Array.isArray(commandMode ? data.commands : data.queries));
  const database = new Map<string, Table>();
  for (const item of data.database as unknown[]) {
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
      row.forEach((v: unknown, i: number) =>
        requireThat(v === null ? columns[i].nullable : typed(v, columns[i].type)),
      );
    }
    database.set(item.name, {
      name: item.name,
      columns,
      rows: item.rows as Value[][],
    });
  }
  if (commandMode) return { database, commands: data.commands as unknown[] };
  const queries: InputQuery[] = [];
  for (const q of data.queries as unknown[]) {
    fields(q, ["sql", "optimize"]);
    requireThat(typeof q.sql === "string" && typeof q.optimize === "boolean");
    queries.push({ sql: q.sql, optimize: q.optimize });
  }
  return { database, queries };
}
