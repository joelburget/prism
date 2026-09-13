/** Stable relational operators and real constant-fold / scan-filter rewrites. */
import { aggregates } from "./model.ts";
import type { Expr, Plan, Sort, Value } from "./model.ts";
import { walk } from "./binder.ts";
/** Orders two non-null values of the same type; false sorts before true. */
export function rank(a: Value, b: Value): number {
  return a === b ? 0 : (a as number) < (b as number) ? -1 : 1;
}
/**
 * `slots` carries aggregate results that were maintained incrementally; when it
 * is absent the aggregate is folded over `group` exactly as the one-shot engine
 * has always done.
 */
export function evaluate(
  e: Expr,
  row: Value[],
  group: Value[][] = [],
  slots?: Value[],
): Value {
  if (e.op === "lit") return e.value as Value;
  if (e.op === "col") return row[e.index!];
  if (aggregates.has(e.op)) {
    if (slots && e.slot !== undefined) return slots[e.slot];
    if (e.op === "COUNT" && !e.args.length) return group.length;
    const vs = group
      .map((r) => evaluate(e.args[0], r))
      .filter((v) => v !== null);
    if (e.op === "COUNT") return vs.length;
    if (!vs.length) return null;
    if (e.op === "SUM")
      return vs.reduce<number>((n, v) => n + (v as number), 0);
    return vs.reduce((a, b) =>
      rank(a, b) === (e.op === "MIN" ? -1 : 1) ? a : b,
    );
  }
  if (e.op === "COALESCE") {
    for (const a of e.args) {
      const v = evaluate(a, row, group, slots);
      if (v !== null) return v;
    }
    return null;
  }
  const a = evaluate(e.args[0], row, group, slots);
  if (e.op === "isnull") return a === null;
  if (e.op === "isnotnull") return a !== null;
  if (e.op === "NOT") return a === null ? null : !a;
  const b = evaluate(e.args[1], row, group, slots);
  /** AND/OR are the only operators that can absorb an UNKNOWN operand. */
  if (e.op === "AND")
    return a === false || b === false ? false : a === null || b === null ? null : true;
  if (e.op === "OR")
    return a === true || b === true ? true : a === null || b === null ? null : false;
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
      return rank(a, b) < 0;
    case "<=":
      return rank(a, b) <= 0;
    case ">":
      return rank(a, b) > 0;
    case ">=":
      return rank(a, b) >= 0;
    default:
      throw new Error(`Unknown bound operator ${e.op}`);
  }
}
/** Only TRUE passes a predicate; FALSE and UNKNOWN are both rejected. */
export function holds(
  e: Expr,
  row: Value[],
  group: Value[][] = [],
  slots?: Value[],
): boolean {
  return evaluate(e, row, group, slots) === true;
}
/** Shared ORDER BY comparator; ties are left to the caller's encounter order. */
export function compare(order: Sort[], a: Value[], b: Value[]): number {
  for (const { key, desc, nullsFirst } of order) {
    const i = key as number;
    if (a[i] === null || b[i] === null) {
      if (a[i] === b[i]) continue;
      return (a[i] === null ? -1 : 1) * (nullsFirst ? 1 : -1);
    }
    const c = rank(a[i], b[i]);
    if (c) return desc ? -c : c;
  }
  return 0;
}
function alike(a: Expr, b: Expr): boolean {
  return (
    a.op === b.op &&
    a.index === b.index &&
    JSON.stringify(a.value ?? null) === JSON.stringify(b.value ?? null) &&
    a.args.length === b.args.length &&
    a.args.every((x, i) => alike(x, b.args[i]))
  );
}
/** Aggregates depend on the surrounding group, so they are never rewritten. */
function scalar(e: Expr): boolean {
  return ![...walk(e)].some((n) => aggregates.has(n.op));
}
function constant(value: Value, type?: Expr["type"]): Expr {
  return { op: "lit", value, args: [], type, nullable: value === null };
}
/**
 * Tautologies only hold over operands that cannot be NULL. A nullable column,
 * or any column on the padded side of an outer join, makes `x = x` UNKNOWN and
 * `p OR NOT p` UNKNOWN, so the binder's nullability flags gate these rules.
 */
function simplify(e: Expr): Expr {
  const [x, y] = e.args;
  if (!y || !scalar(e)) return e;
  if (["=", "<>"].includes(e.op) && !x.nullable && !y.nullable && alike(x, y))
    return constant(e.op === "=", "bool");
  if (["AND", "OR"].includes(e.op)) {
    const opposed = (p: Expr, n: Expr) =>
      n.op === "NOT" && !p.nullable && alike(p, n.args[0]);
    if (opposed(x, y) || opposed(y, x))
      return constant(e.op === "OR", "bool");
  }
  return e;
}
function fold(e: Expr): Expr {
  e.args = e.args.map(fold);
  return !aggregates.has(e.op) &&
    !["lit", "col"].includes(e.op) &&
    e.args.every((a) => a.op === "lit")
    ? constant(evaluate(e, []), e.type)
    : simplify(e);
}
function conjuncts(e: Expr): Expr[] {
  return e.op === "AND" ? e.args.flatMap(conjuncts) : [e];
}
export function optimize(plan: Plan): Plan {
  const q = plan.query;
  q.select = q.select.map(([e, a]) => [fold(e), a]);
  for (const j of q.joins) j.on = fold(j.on);
  if (q.having) q.having = fold(q.having);
  if (q.where) {
    const width = plan.tables.reduce((n, t) => n + t.columns.length, 0);
    const padded = Array<Value>(width).fill(null);
    const parts = conjuncts(fold(q.where)).map((e) => ({
      e,
      sources: new Set(
        [...walk(e)].filter((n) => n.op === "col").map((n) => n.source!),
      ),
    }));
    /**
     * A WHERE conjunct that rejects an all-NULL row also rejects every padded
     * row of the outer join that produced those NULLs, so the join can become
     * an inner join; that in turn re-enables pushdown into its right input.
     */
    for (const { e, sources } of parts) {
      if (sources.size !== 1) continue;
      const i = [...sources][0];
      if (i > 0 && q.joins[i - 1].outer && !holds(e, padded))
        q.joins[i - 1].outer = false;
    }
    const remaining: Expr[] = [];
    for (const { e, sources } of parts) {
      const i = [...sources][0];
      /** Never push below an outer join: padding happens after the scan. */
      if (
        plan.tables.length > 1 &&
        sources.size === 1 &&
        (i === 0 || !q.joins[i - 1].outer)
      )
        plan.filters[i].push(e);
      else remaining.push(e);
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
      plan.filters[i].every((p) => holds(p, [...Array<Value>(offset), ...r])),
    );
    offset += t.columns.length;
    return rows;
  });
  let rows = scans[0];
  q.joins.forEach((join, i) => {
    const pad = Array<Value>(plan.tables[i + 1].columns.length).fill(null);
    rows = rows.flatMap((l) => {
      const matched = scans[i + 1]
        .map((r) => [...l, ...r])
        .filter((r) => holds(join.on, r));
      return matched.length || !join.outer ? matched : [[...l, ...pad]];
    });
  });
  if (q.where) rows = rows.filter((r) => holds(q.where!, r));
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
    .filter(([r, g]) => !q.having || holds(q.having, r, g))
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
  projected.sort((a, b) => compare(q.order, a, b));
  return {
    columns: q.select.map(([, a]) => a),
    rows: projected.slice(
      q.offset,
      q.limit === undefined ? undefined : q.offset + q.limit,
    ),
  };
}
