/** Binding and static validation precede all rewrites, even on empty input. */
import { aggregates, requireThat as need } from "./model.ts";
import type { Expr, Query, Plan, Table, ScalarType } from "./model.ts";
interface BoundColumn {
  qualifier: string;
  name: string;
  type: ScalarType;
  source: number;
}
export function* walk(e: Expr): Generator<Expr> {
  yield e;
  for (const a of e.args) yield* walk(a);
}
function unify(a: ScalarType | undefined, b: ScalarType | undefined): ScalarType | undefined {
  if (a === undefined) return b;
  if (b === undefined) return a;
  need(a === b, "TYPE_ERROR");
  return a;
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
    e.source = c.source;
  } else if (e.op === "lit") {
    // e.type stays undefined for a NULL literal.
  } else if (e.op === "ISNULL" || e.op === "ISNOTNULL") {
    check(e.args[0], columns, allow, inside);
    e.type = "bool";
  } else if (e.op === "COALESCE") {
    for (const a of e.args) check(a, columns, allow, inside);
    e.type = e.args.reduce<ScalarType | undefined>((t, a) => unify(t, a.type), undefined);
  } else {
    const agg = aggregates.has(e.op);
    need(!agg || (allow && !inside), "INVALID_AGGREGATION");
    for (const a of e.args) check(a, columns, allow, inside || agg);
    const ts = e.args.map((a) => a.type);
    if (e.op === "COUNT") e.type = "int";
    else if (e.op === "SUM") {
      need(ts[0] === undefined || ts[0] === "int", "TYPE_ERROR");
      e.type = "int";
    } else if (["+", "-", "*"].includes(e.op)) {
      need(
        ts.every((t) => t === undefined || t === "int"),
        "TYPE_ERROR",
      );
      e.type = "int";
    } else if (["MIN", "MAX"].includes(e.op)) {
      need(ts[0] === undefined || ts[0] === "int" || ts[0] === "text", "TYPE_ERROR");
      e.type = ts[0] ?? "int";
    } else if (["NOT", "AND", "OR"].includes(e.op)) {
      need(
        ts.every((t) => t === undefined || t === "bool"),
        "TYPE_ERROR",
      );
      e.type = "bool";
    } else {
      const combined = unify(ts[0], ts[1]);
      need(
        combined === undefined || combined !== "bool" || ["=", "<>"].includes(e.op),
        "TYPE_ERROR",
      );
      e.type = "bool";
    }
  }
  need(!predicate || e.type === undefined || e.type === "bool", "TYPE_ERROR");
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
    columns.push(...t.columns.map((c) => ({ name: c.name, type: c.type, qualifier, source })));
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
  q.order.forEach((o) => {
    const i = aliases.indexOf(o.alias);
    need(i >= 0, "UNKNOWN_COLUMN");
    o.index = i;
  });
  return { query: q, tables, filters: tables.map(() => []), aggregate };
}
