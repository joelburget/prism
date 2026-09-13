import { readFileSync } from "node:fs";
import { DomainError, evaluateRequest } from "./workflow.ts";

let response: unknown;
try {
  const request = JSON.parse(
    readFileSync(0, "utf8"),
    (_key: string, value: unknown, context?: { source?: string }) => {
      // Preserve the protocol's integer-token rule before JSON's numeric coercion loses it.
      if (
        typeof value === "number" &&
        context?.source &&
        !/^-?(0|[1-9][0-9]*)$/.test(context.source)
      )
        throw new DomainError("INVALID_INPUT");
      return value;
    },
  ) as { input?: unknown };
  response = { ok: true, result: evaluateRequest(request?.input) };
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else if (error instanceof SyntaxError)
    response = { ok: false, error: { code: "INVALID_INPUT" } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
