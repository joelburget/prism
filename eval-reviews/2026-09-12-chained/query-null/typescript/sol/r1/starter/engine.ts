/** Stable relational operators and real constant-fold / scan-filter rewrites. */
import { aggregates } from "./model.ts";
import type { Expr, Plan, Value } from "./model.ts";
import { walk } from "./binder.ts";
export function evaluate(e: Expr, row: Value[], group: Value[][] = []): Value {
  if (e.op === "lit") return e.value as Value;
  if (e.op === "col") return row[e.index!];
  if (aggregates.has(e.op)) {
    if (e.op === "COUNT")
      return e.args.length
        ? group.reduce((n, r) => n + (evaluate(e.args[0], r) === null ? 0 : 1), 0)
        : group.length;
    const vs = group
      .map((r) => evaluate(e.args[0], r))
      .filter((v) => v !== null);
    if (!vs.length) return null;
    if (e.op === "SUM")
      return vs.reduce<number>((n, v) => n + (v as number), 0);
    return vs.reduce((a, b) =>
      e.op === "MIN" ? (a < b ? a : b) : a > b ? a : b,
    );
  }
  if (e.op === "COALESCE") {
    for (const arg of e.args) {
      const value = evaluate(arg, row, group);
      if (value !== null) return value;
    }
    return null;
  }
  const a = evaluate(e.args[0], row, group);
  if (e.op === "IS NULL") return a === null;
  if (e.op === "IS NOT NULL") return a !== null;
  if (e.op === "NOT") return a === null ? null : !a;
  const b = evaluate(e.args[1], row, group);
  if (e.op === "AND") {
    if (a === false || b === false) return false;
    return a === null || b === null ? null : true;
  }
  if (e.op === "OR") {
    if (a === true || b === true) return true;
    return a === null || b === null ? null : false;
  }
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
function fold(e: Expr): Expr {
  e.args = e.args.map(fold);
  return !aggregates.has(e.op) &&
    !["lit", "col"].includes(e.op) &&
    e.args.every((a) => a.op === "lit")
    ? { op: "lit", value: evaluate(e, []), args: [], type: e.type }
    : e;
}
function conjuncts(e: Expr): Expr[] {
  return e.op === "AND" ? e.args.flatMap(conjuncts) : [e];
}
export function optimize(plan: Plan): Plan {
  const q = plan.query;
  q.select = q.select.map(([e, a]) => [fold(e), a]);
  q.joins = q.joins.map((j) => ({ ...j, on: fold(j.on) }));
  if (q.having) q.having = fold(q.having);
  if (q.where) {
    const remaining: Expr[] = [];
    for (const e of conjuncts(fold(q.where))) {
      const sources = new Set(
        [...walk(e)].filter((n) => n.op === "col").map((n) => n.source!),
      );
      if (
        plan.tables.length > 1 &&
        q.joins.every((j) => j.kind === "inner") &&
        sources.size === 1
      )
        plan.filters[[...sources][0]].push(e);
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
      plan.filters[i].every((p) =>
        evaluate(p, [...Array<Value>(offset).fill(null), ...r]) === true,
      ),
    );
    offset += t.columns.length;
    return rows;
  });
  let rows = scans[0];
  q.joins.forEach((join, i) => {
    rows = rows.flatMap((left) => {
      const matches = scans[i + 1]
        .map((right) => [...left, ...right])
        .filter((row) => evaluate(join.on, row) === true);
      return matches.length || join.kind === "inner"
        ? matches
        : [[...left, ...Array<Value>(plan.tables[i + 1].columns.length).fill(null)]];
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
    for (const [index, desc, explicitNullsFirst] of q.order) {
      const i = index as number;
      if (a[i] === null || b[i] === null) {
        if (a[i] === b[i]) continue;
        const nullsFirst = explicitNullsFirst ?? desc;
        return a[i] === null ? (nullsFirst ? -1 : 1) : nullsFirst ? 1 : -1;
      }
      const c = a[i] < b[i] ? -1 : a[i] > b[i] ? 1 : 0;
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
