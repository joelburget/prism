/** Syntax tree, typed logical plan, and protocol validation. */
/** SQL NULL is represented by JavaScript `null` in every value position. */
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
  /** Absent for an untyped NULL literal, which adopts the type of its context. */
  type?: ScalarType;
  index?: number;
  source?: number;
  /** Conservative static answer to "can this ever evaluate to NULL?". */
  nullable?: boolean;
  /** Slot of an incrementally maintained aggregate, assigned per view. */
  slot?: number;
}
export interface Join {
  on: Expr;
  /** LEFT [OUTER] JOIN pads unmatched left rows instead of dropping them. */
  outer: boolean;
}
export interface Sort {
  /** Output alias before binding, projected column index afterwards. */
  key: string | number;
  desc: boolean;
  nullsFirst: boolean;
}
export interface Query {
  select: [Expr, string][];
  sources: [string, string][];
  joins: Join[];
  where?: Expr;
  groups: Expr[];
  having?: Expr;
  order: Sort[];
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
/** A checkpoint-two request carries either the old `queries` or new `commands`. */
export interface Request {
  database: Map<string, Table>;
  queries?: InputQuery[];
  commands?: unknown[];
}
/** Table names in change records are plain names: reserved words reach lookup. */
export const tableName = /^[a-z_][a-z0-9_]*$/;
export const viewName = /^[a-z][a-z0-9-]{0,39}$/;
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
  code = "INVALID_INPUT",
): asserts x is Record<string, unknown> {
  requireThat(x !== null && typeof x === "object" && !Array.isArray(x), code);
  requireThat(
    Object.keys(x as object).length === names.length &&
      names.every((k) => Object.hasOwn(x as object, k)),
    code,
  );
}
export function validate(request: unknown): Request {
  object(request);
  requireThat(request.protocol_version === 1 && request.task === "query-null");
  const data = request.input;
  object(data);
  /** Exactly one of the two input forms; mixing or omitting either is invalid. */
  const batched = Object.hasOwn(data, "commands");
  fields(data, ["database", batched ? "commands" : "queries"]);
  requireThat(Array.isArray(data.database));
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
      row.forEach((v: unknown, i: number) => {
        if (v === null) requireThat(columns[i].nullable);
        else
          requireThat(
            columns[i].type === "int"
              ? typeof v === "number" && Number.isInteger(v)
              : typeof v === (columns[i].type === "text" ? "string" : "boolean"),
          );
      });
    }
    database.set(item.name, {
      name: item.name,
      columns,
      rows: item.rows as Value[][],
    });
  }
  if (batched) {
    requireThat(Array.isArray(data.commands));
    const commands = data.commands as unknown[];
    requireThat(commands.length <= 2000);
    return { database, commands };
  }
  requireThat(Array.isArray(data.queries));
  const queries: InputQuery[] = [];
  for (const q of data.queries as unknown[]) {
    fields(q, ["sql", "optimize"]);
    requireThat(typeof q.sql === "string" && typeof q.optimize === "boolean");
    queries.push({ sql: q.sql, optimize: q.optimize });
  }
  return { database, queries };
}
