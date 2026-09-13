/** Binding and static validation precede all rewrites, even on empty input. */
import { aggregates, requireThat as need } from "./model.ts";
import type { Expr, Query, Plan, Table, ScalarType } from "./model.ts";
interface BoundColumn {
  qualifier: string;
  name: string;
  type: ScalarType;
  nullable: boolean;
  source: number;
}
export function* walk(e: Expr): Generator<Expr> {
  yield e;
  for (const a of e.args) yield* walk(a);
}
/** An untyped NULL literal has no type and unifies with whatever is required. */
function fits(t: ScalarType | undefined, ...allowed: ScalarType[]): boolean {
  return t === undefined || allowed.includes(t);
}
function check(
  e: Expr,
  columns: BoundColumn[],
  allow: boolean,
  inside = false,
  predicate = false,
): void {
  if (e.op === "col") {
    const [qualifier, name] = e.value as [string, string];
    const matches = columns
      .map((c, i) => ({ c, i }))
      .filter(
        ({ c }) => c.name === name && (!qualifier || c.qualifier === qualifier),
      );
    need(matches.length > 0, "UNKNOWN_COLUMN");
    need(matches.length === 1, "AMBIGUOUS_COLUMN");
    const { c, i } = matches[0];
    e.index = i;
    e.type = c.type;
    e.nullable = c.nullable;
    e.source = c.source;
  } else if (e.op === "lit") {
    e.nullable = e.value === null;
  } else {
    const agg = aggregates.has(e.op);
    need(!agg || (allow && !inside), "INVALID_AGGREGATION");
    for (const a of e.args) check(a, columns, allow, inside || agg);
    const ts = e.args.map((a) => a.type);
    /** Anything reachable through a NULL operand may itself be NULL. */
    const spreads = e.args.some((a) => a.nullable);
    if (e.op === "isnull" || e.op === "isnotnull") {
      e.type = "bool";
      e.nullable = false;
    } else if (e.op === "COUNT") {
      e.type = "int";
      e.nullable = false;
    } else if (["SUM", "+", "-", "*"].includes(e.op)) {
      need(
        ts.every((t) => fits(t, "int")),
        "TYPE_ERROR",
      );
      e.type = "int";
      e.nullable = e.op === "SUM" || spreads;
    } else if (["MIN", "MAX"].includes(e.op)) {
      need(fits(ts[0], "int", "text"), "TYPE_ERROR");
      e.type = ts[0];
      e.nullable = true;
    } else if (["NOT", "AND", "OR"].includes(e.op)) {
      need(
        ts.every((t) => fits(t, "bool")),
        "TYPE_ERROR",
      );
      e.type = "bool";
      e.nullable = spreads;
    } else if (e.op === "COALESCE") {
      const known = ts.filter((t) => t !== undefined);
      need(
        known.every((t) => t === known[0]),
        "TYPE_ERROR",
      );
      e.type = known[0];
      e.nullable = e.args.every((a) => a.nullable);
    } else {
      const common = ts[0] === undefined || ts[1] === undefined;
      need(
        (common || ts[0] === ts[1]) &&
          (![ts[0], ts[1]].includes("bool") || ["=", "<>"].includes(e.op)),
        "TYPE_ERROR",
      );
      e.type = "bool";
      e.nullable = spreads;
    }
  }
  need(!predicate || fits(e.type, "bool"), "TYPE_ERROR");
}
function grouped(e: Expr, keys: Set<number>): void {
  if (aggregates.has(e.op)) return;
  need(e.op !== "col" || keys.has(e.index!), "INVALID_AGGREGATION");
  for (const a of e.args) grouped(a, keys);
}
export function bind(q: Query, database: Map<string, Table>): Plan {
  const aliases = q.select.map(([, a]) => a);
  need(new Set(aliases).size === aliases.length, "DUPLICATE_ALIAS");
  need(
    new Set(q.sources.map(([, a]) => a)).size === q.sources.length,
    "DUPLICATE_ALIAS",
  );
  const tables: Table[] = [],
    columns: BoundColumn[] = [];
  q.sources.forEach(([name, qualifier], source) => {
    const t = database.get(name);
    need(t, "UNKNOWN_TABLE");
    tables.push(t);
    /** An outer join can NULL every column of its right input, declared or not. */
    const padded = source > 0 && q.joins[source - 1].outer;
    columns.push(
      ...t.columns.map((c) => ({
        ...c,
        nullable: c.nullable || padded,
        qualifier,
        source,
      })),
    );
    if (source) check(q.joins[source - 1].on, columns, false, false, true);
  });
  q.groups.forEach((e) => check(e, columns, false));
  const keys = new Set(q.groups.map((e) => e.index!));
  need(keys.size === q.groups.length, "INVALID_AGGREGATION");
  if (q.where) check(q.where, columns, false, false, true);
  q.select.forEach(([e]) => check(e, columns, true));
  if (q.having) check(q.having, columns, true, false, true);
  const roots = [...q.select.map(([e]) => e), ...(q.having ? [q.having] : [])];
  const nodes = roots.flatMap((e) => [...walk(e)]);
  const aggregate =
    q.groups.length > 0 || nodes.some((e) => aggregates.has(e.op));
  need(!q.having || aggregate, "INVALID_AGGREGATION");
  if (aggregate) roots.forEach((e) => grouped(e, keys));
  q.order = q.order.map((o) => {
    const i = aliases.indexOf(o.key as string);
    need(i >= 0, "UNKNOWN_COLUMN");
    return { ...o, key: i };
  });
  return { query: q, tables, filters: tables.map(() => []), aggregate };
}
