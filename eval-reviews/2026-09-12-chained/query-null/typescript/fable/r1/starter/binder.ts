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
const comparisons = new Set(["=", "<>", "<", "<=", ">", ">="]);
/**
 * Type-check and annotate `e`. `padded` holds the sources whose columns may
 * have been NULL-padded by a LEFT JOIN at this point of the query; inside the
 * ON clause of a join the right-hand table is not yet padded.
 */
function check(
  e: Expr,
  columns: BoundColumn[],
  allow: boolean,
  padded: Set<number>,
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
    e.nullable = c.nullable || padded.has(c.source);
  } else if (e.op === "lit") {
    e.nullable = e.value === null;
  } else {
    const agg = aggregates.has(e.op);
    need(!agg || (allow && !inside), "INVALID_AGGREGATION");
    for (const a of e.args) check(a, columns, allow, padded, inside || agg);
    // Untyped NULL literals (type undefined) adopt whatever type is required.
    const ts = e.args
      .map((a) => a.type)
      .filter((t): t is ScalarType => t !== undefined);
    const uniform = ts.every((t) => t === ts[0]);
    const anyNullable = e.args.some((a) => a.nullable);
    if (e.op === "COUNT") {
      e.type = "int";
      e.nullable = false;
    } else if (["SUM", "+", "-", "*"].includes(e.op)) {
      need(
        ts.every((t) => t === "int"),
        "TYPE_ERROR",
      );
      e.type = "int";
      e.nullable = e.op === "SUM" || anyNullable;
    } else if (["MIN", "MAX"].includes(e.op)) {
      need(
        ts.every((t) => t !== "bool"),
        "TYPE_ERROR",
      );
      e.type = ts[0];
      e.nullable = true;
    } else if (["NOT", "AND", "OR"].includes(e.op)) {
      need(
        ts.every((t) => t === "bool"),
        "TYPE_ERROR",
      );
      e.type = "bool";
      e.nullable = anyNullable;
    } else if (e.op === "IS NULL" || e.op === "IS NOT NULL") {
      e.type = "bool";
      e.nullable = false;
    } else if (e.op === "COALESCE") {
      need(uniform, "TYPE_ERROR");
      e.type = ts[0];
      e.nullable = e.args.every((a) => a.nullable);
    } else if (comparisons.has(e.op)) {
      need(
        uniform && (ts[0] !== "bool" || ["=", "<>"].includes(e.op)),
        "TYPE_ERROR",
      );
      e.type = "bool";
      e.nullable = anyNullable;
    } else throw new Error(`Unknown operator ${e.op}`);
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
  const padded = new Set<number>();
  q.sources.forEach(([name, qualifier], source) => {
    const t = database.get(name);
    need(t, "UNKNOWN_TABLE");
    tables.push(t);
    columns.push(...t.columns.map((c) => ({ ...c, qualifier, source })));
    if (source) {
      check(q.joins[source - 1], columns, false, padded, false, true);
      if (q.outer[source - 1]) padded.add(source);
    }
  });
  q.groups.forEach((e) => check(e, columns, false, padded));
  const keys = new Set(q.groups.map((e) => e.index!));
  need(keys.size === q.groups.length, "INVALID_AGGREGATION");
  if (q.where) check(q.where, columns, false, padded, false, true);
  q.select.forEach(([e]) => check(e, columns, true, padded));
  if (q.having) check(q.having, columns, true, padded, false, true);
  const roots = [...q.select.map(([e]) => e), ...(q.having ? [q.having] : [])];
  const nodes = roots.flatMap((e) => [...walk(e)]);
  const aggregate =
    q.groups.length > 0 || nodes.some((e) => aggregates.has(e.op));
  need(!q.having || aggregate, "INVALID_AGGREGATION");
  if (aggregate) roots.forEach((e) => grouped(e, keys));
  q.order = q.order.map(([a, desc, nullsFirst]) => {
    const i = aliases.indexOf(a as string);
    need(i >= 0, "UNKNOWN_COLUMN");
    return [i, desc, nullsFirst];
  });
  return { query: q, tables, filters: tables.map(() => []), aggregate };
}
