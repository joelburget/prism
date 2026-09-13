import { readFileSync } from "node:fs";
import { DomainError, validate, requireThat } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { ViewDatabase } from "./views.ts";
let response: unknown;
try {
  const request = validate(
    JSON.parse(
      readFileSync(0, "utf8"),
      (_key: string, value: unknown, context?: { source?: string }) => {
        requireThat(
          typeof value !== "number" || !/[.eE]/.test(context?.source ?? ""),
          "INVALID_INPUT",
        );
        return value;
      },
    ) as unknown,
  );
  if (request.mode === "queries") {
    const results = request.queries.map((q) => {
      const plan = bind(new Parser(q.sql).parse(), request.database);
      return execute(q.optimize ? optimize(plan) : plan);
    });
    response = { ok: true, result: { results } };
  } else {
    const db = new ViewDatabase(request.database);
    const results = request.commands.map((c) => db.handle(c));
    response = { ok: true, result: { results } };
  }
} catch (e) {
  if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
  response = {
    ok: false,
    error: { code: e instanceof DomainError ? e.message : "INVALID_INPUT" },
  };
}
process.stdout.write(JSON.stringify(response) + "\n");
