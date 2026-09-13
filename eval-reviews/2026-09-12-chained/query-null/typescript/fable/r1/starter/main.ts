import { readFileSync } from "node:fs";
import { DomainError, validate } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { Database } from "./views.ts";
let response: unknown;
try {
  const request = validate(
    JSON.parse(
      readFileSync(0, "utf8"),
      (_key: string, value: unknown, context?: { source?: string }) => {
        // Non-integer number syntax never denotes an int; NaN fails every check.
        return typeof value === "number" && /[.eE]/.test(context?.source ?? "") ? NaN : value;
      },
    ) as unknown,
  );
  const results = request.commands
    ? new Database(request.database).run(request.commands)
    : request.queries!.map((q) => {
        const plan = bind(new Parser(q.sql).parse(), request.database);
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
