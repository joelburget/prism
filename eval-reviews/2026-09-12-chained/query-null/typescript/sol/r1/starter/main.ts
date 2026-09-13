import { readFileSync } from "node:fs";
import { DomainError, validate, requireThat } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
function rejectDuplicateKeys(text: string): void {
  let i = 0;
  const space = () => {
    while (/\s/.test(text[i] ?? "")) i++;
  };
  const string = (): string => {
    const start = i++;
    while (i < text.length) {
      if (text[i] === "\\") i += 2;
      else if (text[i++] === '"') return JSON.parse(text.slice(start, i)) as string;
    }
    throw new SyntaxError();
  };
  const value = (): void => {
    space();
    if (text[i] === "{") object();
    else if (text[i] === "[") array();
    else if (text[i] === '"') void string();
    else while (i < text.length && !/[,}\]]/.test(text[i])) i++;
    space();
  };
  const object = (): void => {
    i++;
    space();
    const keys = new Set<string>();
    if (text[i] === "}") {
      i++;
      return;
    }
    while (true) {
      const key = string();
      if (keys.has(key)) throw new SyntaxError();
      keys.add(key);
      space();
      if (text[i++] !== ":") throw new SyntaxError();
      value();
      if (text[i] === "}") {
        i++;
        return;
      }
      if (text[i++] !== ",") throw new SyntaxError();
      space();
    }
  };
  const array = (): void => {
    i++;
    space();
    if (text[i] === "]") {
      i++;
      return;
    }
    while (true) {
      value();
      if (text[i] === "]") {
        i++;
        return;
      }
      if (text[i++] !== ",") throw new SyntaxError();
    }
  };
  value();
}
let response: unknown;
try {
  const input = readFileSync(0, "utf8");
  const parsed = JSON.parse(
    input,
    (_key: string, value: unknown, context?: { source?: string }) => {
      requireThat(
        typeof value !== "number" || !/[.eE]/.test(context?.source ?? ""),
        "INVALID_INPUT",
      );
      return value;
    },
  ) as unknown;
  rejectDuplicateKeys(input);
  const [database, queries] = validate(
    parsed,
  );
  const results = queries.map((q) => {
    const plan = bind(new Parser(q.sql).parse(), database);
    return execute(q.optimize ? optimize(plan) : plan);
  });
  response = { ok: true, result: { results } };
} catch (e) {
  if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
  response = {
    ok: false,
    error: { code: e instanceof DomainError ? e.message : "INVALID_INPUT" },
  };
}
process.stdout.write(JSON.stringify(response) + "\n");
