/** Stable relational operators and real constant-fold / scan-filter rewrites. */
import { aggregates } from "./model.ts";
import type { Expr, Plan, ScalarType, Value } from "./model.ts";
import { walk } from "./binder.ts";
type Known = Exclude<Value, null>;
/**
 * Three-valued evaluation. Booleans are represented directly: TRUE is `true`,
 * FALSE is `false`, and UNKNOWN is `null`, the same value as SQL NULL.
 */
export function evaluate(e: Expr, row: Value[], group: Value[][] = []): Value {
  switch (e.op) {
    case "lit":
      return e.value as Value;
    case "col":
      return row[e.index!];
    case "COUNT":
      return e.args.length
        ? group.filter((r) => evaluate(e.args[0], r) !== null).length
        : group.length;
    case "SUM":
    case "MIN":
    case "MAX": {
      const vs = group
        .map((r) => evaluate(e.args[0], r))
        .filter((v): v is Known => v !== null);
      if (!vs.length) return null;
      if (e.op === "SUM") return vs.reduce<number>((n, v) => n + (v as number), 0);
      return vs.reduce((a, b) => (e.op === "MIN" ? (b < a ? b : a) : b > a ? b : a));
    }
    case "COALESCE":
      for (const a of e.args) {
        const v = evaluate(a, row, group);
        if (v !== null) return v;
      }
      return null;
    case "IS NULL":
      return evaluate(e.args[0], row, group) === null;
    case "IS NOT NULL":
      return evaluate(e.args[0], row, group) !== null;
    case "NOT": {
      const a = evaluate(e.args[0], row, group);
      return a === null ? null : !a;
    }
    case "AND": {
      const a = evaluate(e.args[0], row, group);
      const b = evaluate(e.args[1], row, group);
      if (a === false || b === false) return false;
      return a === null || b === null ? null : true;
    }
    case "OR": {
      const a = evaluate(e.args[0], row, group);
      const b = evaluate(e.args[1], row, group);
      if (a === true || b === true) return true;
      return a === null || b === null ? null : false;
    }
  }
  const a = evaluate(e.args[0], row, group);
  const b = evaluate(e.args[1], row, group);
  if (a === null || b === null) return null;
  switch (e.op) {
    case "+":
      return (a as number) + (b as number);
    case "-":
      return (a as number) - (b as number);
    case "*":
      return (a as number) * (b as number);
    case "=":
      return a === b;
    case "<>":
      return a !== b;
    case "<":
      return a < b;
    case "<=":
      return a <= b;
    case ">":
      return a > b;
    case ">=":
      return a >= b;
    default:
      throw new Error(`Unknown bound operator ${e.op}`);
  }
}
function literal(value: Value, type?: ScalarType): Expr {
  return { op: "lit", value, args: [], type, nullable: value === null };
}
function isLiteral(e: Expr, value: Value): boolean {
  return e.op === "lit" && e.value === value;
}
/** Structural equality of bound, aggregate-free deterministic expressions. */
function same(a: Expr, b: Expr): boolean {
  return (
    a.op === b.op &&
    a.index === b.index &&
    JSON.stringify(a.value ?? null) === JSON.stringify(b.value ?? null) &&
    a.args.length === b.args.length &&
    a.args.every((x, i) => same(x, b.args[i]))
  );
}
/** Operators that yield NULL whenever any operand is NULL. */
const strict = new Set(["+", "-", "*", "=", "<>", "<", "<=", ">", ">=", "NOT"]);
/**
 * Bottom-up simplification that is sound under three-valued logic. Rewrites
 * that would be tautologies in two-valued logic (`x = x`, `p OR NOT p`,
 * `x IS NOT NULL`) fire only when the binder proved the operand non-nullable.
 */
function fold(e: Expr): Expr {
  e.args = e.args.map(fold);
  if (aggregates.has(e.op) || e.op === "lit" || e.op === "col") return e;
  if (e.op === "COALESCE") {
    // Drop NULL literals; nothing after the first non-nullable argument matters.
    const kept: Expr[] = [];
    for (const a of e.args) {
      if (isLiteral(a, null)) continue;
      kept.push(a);
      if (!a.nullable) break;
    }
    if (kept.length === 0) return literal(null, e.type);
    if (kept.length === 1 && kept[0].type === e.type) return kept[0];
    e.args = kept;
  }
  if (e.args.every((a) => a.op === "lit")) return literal(evaluate(e, []), e.type);
  const [a, b] = e.args;
  if (strict.has(e.op) && e.args.some((x) => isLiteral(x, null)))
    return literal(null, e.type);
  if (e.op === "NOT" && a.op === "NOT") return a.args[0];
  if (e.op === "AND" || e.op === "OR") {
    // FALSE absorbs AND and TRUE absorbs OR even against UNKNOWN; the other
    // constant is an identity. NULL literals cannot be removed.
    const absorbing = e.op === "OR";
    if (e.args.some((x) => isLiteral(x, absorbing))) return literal(absorbing, "bool");
    if (isLiteral(a, !absorbing)) return b;
    if (isLiteral(b, !absorbing)) return a;
    const complementary =
      (b.op === "NOT" && same(a, b.args[0])) || (a.op === "NOT" && same(a.args[0], b));
    if (complementary && !a.nullable) return literal(absorbing, "bool");
  }
  if (e.op === "=" && !a.nullable && same(a, b)) return literal(true, "bool");
  if (e.op === "IS NULL" && !a.nullable) return literal(false, "bool");
  if (e.op === "IS NOT NULL" && !a.nullable) return literal(true, "bool");
  return e;
}
function conjuncts(e: Expr): Expr[] {
  return e.op === "AND" ? e.args.flatMap(conjuncts) : [e];
}
export function optimize(plan: Plan): Plan {
  const q = plan.query;
  q.select = q.select.map(([e, a]) => [fold(e), a]);
  q.joins = q.joins.map(fold);
  if (q.having) q.having = fold(q.having);
  if (q.where) {
    const width = plan.tables.reduce((n, t) => n + t.columns.length, 0);
    const remaining: Expr[] = [];
    for (const e of conjuncts(fold(q.where))) {
      if (isLiteral(e, true)) continue;
      const sources = new Set(
        [...walk(e)].filter((n) => n.op === "col").map((n) => n.source!),
      );
      if (plan.tables.length > 1 && sources.size === 1) {
        const s = [...sources][0];
        if (s > 0 && q.outer[s - 1]) {
          // The source may be NULL-padded. A conjunct over only its columns can
          // move below the join solely when it rejects the all-NULL padding, in
          // which case the LEFT JOIN is equivalent to an INNER JOIN.
          if (evaluate(e, Array<Value>(width).fill(null)) === true) {
            remaining.push(e);
            continue;
          }
          q.outer[s - 1] = false;
        }
        plan.filters[s].push(e);
      } else remaining.push(e);
    }
    q.where = remaining.reduce<Expr | undefined>(
      (a, e) => (a ? { op: "AND", args: [a, e], type: "bool" } : e),
      undefined,
    );
  }
  return plan;
}
export function execute(plan: Plan): { columns: string[]; rows: Value[][] } {
  const q = plan.query;
  let offset = 0;
  const scans = plan.tables.map((t, i) => {
    const rows = t.rows.filter((r) =>
      plan.filters[i].every(
        (p) => evaluate(p, [...Array<Value>(offset), ...r]) === true,
      ),
    );
    offset += t.columns.length;
    return rows;
  });
  let rows = scans[0];
  q.joins.forEach((on, i) => {
    const padding = Array<Value>(plan.tables[i + 1].columns.length).fill(null);
    rows = rows.flatMap((l) => {
      const matched = scans[i + 1]
        .map((r) => [...l, ...r])
        .filter((r) => evaluate(on, r) === true);
      return matched.length || !q.outer[i] ? matched : [[...l, ...padding]];
    });
  });
  if (q.where) rows = rows.filter((r) => evaluate(q.where!, r) === true);
  let units: [Value[], Value[][]][];
  if (plan.aggregate) {
    const groups = new Map<string, Value[][]>();
    for (const row of rows) {
      const key = JSON.stringify(q.groups.map((e) => evaluate(e, row)));
      const g = groups.get(key);
      if (g) g.push(row);
      else groups.set(key, [row]);
    }
    if (!q.groups.length && !rows.length) groups.set("[]", []);
    units = [...groups.values()].map((g) => [g[0] ?? [], g]);
  } else units = rows.map((r) => [r, []]);
  let projected = units
    .filter(([r, g]) => !q.having || evaluate(q.having, r, g) === true)
    .map(([r, g]) => q.select.map(([e]) => evaluate(e, r, g)));
  if (q.distinct) {
    const seen = new Set<string>();
    projected = projected.filter((r) => {
      const key = JSON.stringify(r);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  projected.sort((a, b) => {
    for (const [index, desc, nullsFirst] of q.order) {
      const x = a[index as number],
        y = b[index as number];
      if (x === null || y === null) {
        if (x === y) continue;
        return (x === null) === nullsFirst ? -1 : 1;
      }
      const c = x < y ? -1 : x > y ? 1 : 0;
      if (c) return desc ? -c : c;
    }
    return 0;
  });
  return {
    columns: q.select.map(([, a]) => a),
    rows: projected.slice(
      q.offset,
      q.limit === undefined ? undefined : q.offset + q.limit,
    ),
  };
}
