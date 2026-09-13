import { readFileSync } from "node:fs";
import { DomainError, validate, requireThat } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { commands } from "./views.ts";
let response: unknown;
try {
  const text = readFileSync(0, "utf8");
  const raw = JSON.parse(text);
  const commandMode = raw?.input && typeof raw.input === "object" && Object.hasOwn(raw.input, "commands");
  const request = JSON.parse(text,
    (_key: string, value: unknown, context?: { source?: string }) => {
      if (typeof value === "number" && /[.eE]/.test(context?.source ?? "")) {
        // Preserve the old integer wire validation. In command mode let the
        // owning validator assign INVALID_COMMAND or INVALID_ROW locally.
        requireThat(commandMode, "INVALID_INPUT");
        return NaN;
      }
      return value;
    },
  ) as Record<string, unknown>;
  let results: unknown[];
  if (commandMode) {
    results = commands(request);
  } else {
    const [database, queries] = validate(request);
    results = queries.map((q) => {
      const plan = bind(new Parser(q.sql).parse(), database);
      return execute(q.optimize ? optimize(plan) : plan);
    });
  }
  response = { ok: true, result: { results } };
} catch (e) {
  if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
  response = {
    ok: false,
    error: { code: e instanceof DomainError ? e.message : "INVALID_INPUT" },
  };
}
process.stdout.write(JSON.stringify(response) + "\n");
