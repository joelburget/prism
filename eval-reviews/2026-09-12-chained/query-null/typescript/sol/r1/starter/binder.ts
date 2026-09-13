/** Binding and static validation precede all rewrites, even on empty input. */
import { aggregates, requireThat as need } from "./model.ts";
import type { Expr, Query, Plan, Table, ScalarType } from "./model.ts";
interface BoundColumn {
  qualifier: string;
  name: string;
  type: ScalarType;
  source: number;
}
const allTypes: ScalarType[] = ["int", "text", "bool"];
function possible(e: Expr): ScalarType[] {
  return e.possible ?? (e.type ? [e.type] : allTypes);
}
function setTypes(e: Expr, types: ScalarType[]): void {
  e.possible = types;
  e.type = types.length === 1 ? types[0] : undefined;
}
function common(args: Expr[], allowed = allTypes): ScalarType[] {
  return allowed.filter((t) => args.every((a) => possible(a).includes(t)));
}
export function* walk(e: Expr): Generator<Expr> {
  yield e;
  for (const a of e.args) yield* walk(a);
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
    e.possible = [c.type];
    e.source = c.source;
  } else if (e.op === "lit") {
    e.possible = e.type ? [e.type] : allTypes;
  } else {
    const agg = aggregates.has(e.op);
    need(!agg || (allow && !inside), "INVALID_AGGREGATION");
    for (const a of e.args) check(a, columns, allow, inside || agg);
    if (e.op === "COUNT") setTypes(e, ["int"]);
    else if (["SUM", "+", "-", "*"].includes(e.op)) {
      need(e.args.every((a) => possible(a).includes("int")), "TYPE_ERROR");
      setTypes(e, ["int"]);
    } else if (["MIN", "MAX"].includes(e.op)) {
      const types = common(e.args, ["int", "text"]);
      need(types.length, "TYPE_ERROR");
      setTypes(e, types);
    } else if (e.op === "COALESCE") {
      const types = common(e.args);
      need(types.length, "TYPE_ERROR");
      setTypes(e, types);
    } else if (e.op === "IS NULL" || e.op === "IS NOT NULL") {
      setTypes(e, ["bool"]);
    } else if (["NOT", "AND", "OR"].includes(e.op)) {
      need(e.args.every((a) => possible(a).includes("bool")), "TYPE_ERROR");
      setTypes(e, ["bool"]);
    } else {
      const allowed: ScalarType[] = ["=", "<>"].includes(e.op)
        ? allTypes
        : ["int", "text"];
      need(common(e.args, allowed).length, "TYPE_ERROR");
      setTypes(e, ["bool"]);
    }
  }
  need(!predicate || possible(e).includes("bool"), "TYPE_ERROR");
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
    columns.push(...t.columns.map((c) => ({ ...c, qualifier, source })));
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
  q.order = q.order.map(([a, d, n]) => {
    const i = aliases.indexOf(a as string);
    need(i >= 0, "UNKNOWN_COLUMN");
    return [i, d, n];
  });
  return { query: q, tables, filters: tables.map(() => []), aggregate };
}
