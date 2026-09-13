/** Tokenizer and precedence parser. Database names are resolved in the binder. */
import { aggregates, reserved, identifier, requireThat as need } from "./model.ts";
import type { Expr, Query } from "./model.ts";
/** Binary operator precedence. IS [NOT] NULL is a postfix operator at level 4. */
const precedence: Record<string, number> = {
  OR: 1,
  AND: 2,
  "=": 4,
  "<>": 4,
  "<": 4,
  ">": 4,
  "<=": 4,
  ">=": 4,
  "+": 5,
  "-": 5,
  "*": 6,
};
const COMPARISON = 4;
const NOT_OPERAND = 3;
export class Parser {
  tokens: string[] = [];
  cursor = 0;
  constructor(sql: string) {
    const token =
      /\s+|'(?:[^']|'')*'|[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+|<>|<=|>=|[(),.;+*=<>-]/y;
    while (token.lastIndex < sql.length) {
      const m = token.exec(sql);
      need(m, "PARSE_ERROR");
      const t = m[0];
      if (!/^\s+$/.test(t))
        this.tokens.push(reserved.has(t.toUpperCase()) ? t.toUpperCase() : t);
    }
    this.tokens.push("$END");
  }
  peek(): string {
    return this.tokens[this.cursor];
  }
  pop(): string {
    const t = this.peek();
    need(t !== "$END", "PARSE_ERROR");
    this.cursor++;
    return t;
  }
  take(t: string): boolean {
    if (this.peek() !== t) return false;
    this.cursor++;
    return true;
  }
  expect(t: string): void {
    need(this.take(t), "PARSE_ERROR");
  }
  name(): string {
    const t = this.pop();
    need(identifier(t), "PARSE_ERROR");
    return t;
  }
  natural(): number {
    const t = this.pop();
    need(/^[0-9]+$/.test(t), "PARSE_ERROR");
    return Number(t);
  }
  reference(): [string, string] {
    const t = this.name();
    return this.take(".") ? [t, this.name()] : ["", t];
  }
  source(): [string, string] {
    const t = this.name();
    return [t, this.take("AS") ? this.name() : t];
  }
  primary(): Expr {
    const t = this.pop();
    if (t === "NOT") return { op: "NOT", args: [this.expr(NOT_OPERAND)] };
    if (t === "(") {
      const e = this.expr();
      this.expect(")");
      return e;
    }
    if (t === "-")
      return { op: "lit", value: -this.natural(), type: "int", args: [] };
    if (/^[0-9]+$/.test(t))
      return { op: "lit", value: Number(t), type: "int", args: [] };
    if (t.startsWith("'"))
      return {
        op: "lit",
        value: t.slice(1, -1).replaceAll("''", "'"),
        type: "text",
        args: [],
      };
    if (t === "TRUE" || t === "FALSE")
      return { op: "lit", value: t === "TRUE", type: "bool", args: [] };
    // An untyped NULL literal: no type until its context demands one.
    if (t === "NULL") return { op: "lit", value: null, args: [] };
    if (t === "COALESCE") {
      this.expect("(");
      const args = [this.expr()];
      while (this.take(",")) args.push(this.expr());
      this.expect(")");
      need(args.length >= 2, "PARSE_ERROR");
      return { op: "COALESCE", args };
    }
    if (aggregates.has(t)) {
      this.expect("(");
      const e: Expr = {
        op: t,
        args: t === "COUNT" && this.take("*") ? [] : [this.expr()],
      };
      this.expect(")");
      return e;
    }
    need(identifier(t), "PARSE_ERROR");
    return {
      op: "col",
      value: this.take(".") ? [t, this.name()] : ["", t],
      args: [],
    };
  }
  expr(minimum = 1): Expr {
    let e = this.primary();
    let compared = false;
    for (;;) {
      const t = this.peek();
      const p = t === "IS" ? COMPARISON : (precedence[t] ?? 0);
      if (p < minimum) break;
      this.pop();
      if (p === COMPARISON) {
        // Comparisons and IS [NOT] NULL share a level and do not chain.
        need(!compared, "PARSE_ERROR");
        compared = true;
      }
      if (t === "IS") {
        const negated = this.take("NOT");
        this.expect("NULL");
        e = { op: negated ? "IS NOT NULL" : "IS NULL", args: [e] };
      } else e = { op: t, args: [e, this.expr(p + 1)] };
    }
    return e;
  }
  parse(): Query {
    const q: Query = {
      select: [],
      sources: [],
      joins: [],
      outer: [],
      groups: [],
      order: [],
      distinct: false,
      offset: 0,
    };
    this.expect("SELECT");
    q.distinct = this.take("DISTINCT");
    do {
      const e = this.expr();
      this.expect("AS");
      q.select.push([e, this.name()]);
    } while (this.take(","));
    this.expect("FROM");
    q.sources.push(this.source());
    while (["INNER", "JOIN", "LEFT"].includes(this.peek())) {
      const outer = this.take("LEFT");
      if (outer) this.take("OUTER");
      else this.take("INNER");
      this.expect("JOIN");
      q.sources.push(this.source());
      this.expect("ON");
      q.joins.push(this.expr());
      q.outer.push(outer);
    }
    if (this.take("WHERE")) q.where = this.expr();
    if (this.take("GROUP")) {
      this.expect("BY");
      do {
        q.groups.push({ op: "col", value: this.reference(), args: [] });
      } while (this.take(","));
    }
    if (this.take("HAVING")) q.having = this.expr();
    if (this.take("ORDER")) {
      this.expect("BY");
      do {
        const a = this.name(),
          desc = this.take("DESC");
        if (!desc) this.take("ASC");
        // NULLs default to LAST when ascending and FIRST when descending.
        let nullsFirst = desc;
        if (this.take("NULLS")) {
          nullsFirst = this.take("FIRST");
          if (!nullsFirst) this.expect("LAST");
        }
        q.order.push([a, desc, nullsFirst]);
      } while (this.take(","));
    }
    if (this.take("LIMIT")) {
      q.limit = this.natural();
      if (this.take("OFFSET")) q.offset = this.natural();
    }
    this.take(";");
    need(this.peek() === "$END", "PARSE_ERROR");
    return q;
  }
}
