/** Stable relational operators and real constant-fold / scan-filter rewrites. */
import { aggregates } from "./model.ts";
import type { Expr, Plan, Value } from "./model.ts";
import { walk } from "./binder.ts";
export function evaluate(e: Expr, row: Value[], group: Value[][] = []): Value {
  if (e.op === "lit") return e.value === undefined ? null : (e.value as Value);
  if (e.op === "col") return row[e.index!];
  if (e.op === "ISNULL") return evaluate(e.args[0], row, group) === null;
  if (e.op === "ISNOTNULL") return evaluate(e.args[0], row, group) !== null;
  if (e.op === "COALESCE") {
    for (const a of e.args) {
      const v = evaluate(a, row, group);
      if (v !== null) return v;
    }
    return null;
  }
  if (aggregates.has(e.op)) {
    if (e.op === "COUNT")
      return e.args.length === 0
        ? group.length
        : group.filter((r) => evaluate(e.args[0], r) !== null).length;
    const vs = group.map((r) => evaluate(e.args[0], r)).filter((v) => v !== null);
    if (e.op === "SUM")
      return vs.length === 0
        ? null
        : vs.reduce<number>((n, v) => n + (v as number), 0);
    if (vs.length === 0) return null;
    return vs.reduce((a, b) =>
      e.op === "MIN" ? (a < b ? a : b) : a > b ? a : b,
    );
  }
  const a = evaluate(e.args[0], row, group);
  if (e.op === "NOT") return a === null ? null : !a;
  const b = evaluate(e.args[1], row, group);
  if (e.op === "AND") {
    if (a === false || b === false) return false;
    if (a === null || b === null) return null;
    return true;
  }
  if (e.op === "OR") {
    if (a === true || b === true) return true;
    if (a === null || b === null) return null;
    return false;
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
    e.op !== "lit" &&
    e.op !== "col" &&
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
      const idx = [...sources][0];
      const rightOfLeftJoin = sources.size === 1 && idx > 0 && q.joins[idx - 1].left;
      if (plan.tables.length > 1 && sources.size === 1 && !rightOfLeftJoin)
        plan.filters[idx].push(e);
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
        evaluate(p, [...Array<Value>(offset), ...r]),
      ),
    );
    offset += t.columns.length;
    return rows;
  });
  let rows = scans[0];
  q.joins.forEach((join, i) => {
    const rightWidth = plan.tables[i + 1].columns.length;
    rows = rows.flatMap((l) => {
      const matched = scans[i + 1]
        .map((r) => [...l, ...r])
        .filter((r) => evaluate(join.on, r) === true);
      if (matched.length === 0 && join.left)
        return [[...l, ...Array<Value>(rightWidth).fill(null)]];
      return matched;
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
  projected = projected
    .map((r, i) => [r, i] as [Value[], number])
    .sort((a, b) => {
      for (const o of q.order) {
        const i = o.index!;
        const av = a[0][i],
          bv = b[0][i];
        if (av === null || bv === null) {
          if (av === bv) continue;
          const c = av === null ? -1 : 1;
          const signed = o.nullsFirst ? c : -c;
          if (signed) return signed;
          continue;
        }
        const c = av < bv ? -1 : av > bv ? 1 : 0;
        if (c) return o.desc ? -c : c;
      }
      return a[1] - b[1];
    })
    .map(([r]) => r);
  return {
    columns: q.select.map(([, a]) => a),
    rows: projected.slice(
      q.offset,
      q.limit === undefined ? undefined : q.offset + q.limit,
    ),
  };
}
