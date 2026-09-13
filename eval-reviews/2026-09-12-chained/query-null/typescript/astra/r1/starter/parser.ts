/** Tokenizer and precedence parser. Database names are resolved in the binder. */
import {
  aggregates,
  reserved,
  identifier,
  requireThat as need,
} from "./model.ts";
import type { Expr, Query } from "./model.ts";
const precedence: Record<string, number> = {
  OR: 1,
  AND: 2,
  IS: 4,
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
  expr(minimum = 1): Expr {
    const t = this.pop();
    let e: Expr;
    if (t === "NULL") e = { op: "lit", value: null, type: "null", args: [] };
    else if (t === "COALESCE") {
      this.expect("(");
      const args = [this.expr()];
      this.expect(",");
      do { args.push(this.expr()); } while (this.take(","));
      this.expect(")");
      e = { op: t, args };
    }
    else if (t === "NOT") e = { op: "NOT", args: [this.expr(3)] };
    else if (t === "(") {
      e = this.expr();
      this.expect(")");
    } else if (t === "-")
      e = { op: "lit", value: -this.natural(), type: "int", args: [] };
    else if (/^[0-9]+$/.test(t))
      e = { op: "lit", value: Number(t), type: "int", args: [] };
    else if (t.startsWith("'"))
      e = {
        op: "lit",
        value: t.slice(1, -1).replaceAll("''", "'"),
        type: "text",
        args: [],
      };
    else if (t === "TRUE" || t === "FALSE")
      e = { op: "lit", value: t === "TRUE", type: "bool", args: [] };
    else if (aggregates.has(t)) {
      this.expect("(");
      e = { op: t, args: t === "COUNT" && this.take("*") ? [] : [this.expr()] };
      this.expect(")");
    } else {
      need(identifier(t), "PARSE_ERROR");
      e = {
        op: "col",
        value: this.take(".") ? [t, this.name()] : ["", t],
        args: [],
      };
    }
    let compared = false;
    while ((precedence[this.peek()] ?? 0) >= minimum) {
      const op = this.pop(),
        p = precedence[op];
      need(!(p === 4 && compared), "PARSE_ERROR");
      if (op === "IS") {
        const negated = this.take("NOT");
        this.expect("NULL");
        e = { op: negated ? "IS NOT NULL" : "IS NULL", args: [e] };
      } else e = { op, args: [e, this.expr(p + 1)] };
      if (p === 4) compared = true;
    }
    return e;
  }
  parse(): Query {
    const q: Query = {
      select: [],
      sources: [],
      joins: [],
      leftJoins: [],
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
      const left = this.take("LEFT");
      if (left) this.take("OUTER");
      else this.take("INNER");
      q.leftJoins.push(left);
      this.expect("JOIN");
      q.sources.push(this.source());
      this.expect("ON");
      q.joins.push(this.expr());
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
        let first = desc;
        if (this.take("NULLS")) {
          first = this.take("FIRST");
          if (!first) this.expect("LAST");
        }
        q.order.push([a, desc, first]);
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
