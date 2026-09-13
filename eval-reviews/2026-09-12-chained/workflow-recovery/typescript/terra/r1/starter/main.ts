import { readFileSync } from "node:fs";
import { DomainError, parseWorkflow, Simulator } from "./workflow.ts";

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
  ) as unknown;
  if (
    request === null || typeof request !== "object" || Array.isArray(request) ||
    !Object.hasOwn(request, "protocol_version") || !Object.hasOwn(request, "task") || !Object.hasOwn(request, "input") ||
    Object.keys(request).some(key => key !== "protocol_version" && key !== "task" && key !== "input") ||
    (request as Record<string, unknown>).protocol_version !== 1 ||
    (request as Record<string, unknown>).task !== "workflow-recovery"
  ) throw new DomainError("INVALID_INPUT");
  response = {
    ok: true,
    result: new Simulator(parseWorkflow((request as Record<string, unknown>).input)).run(),
  };
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else if (error instanceof SyntaxError)
    response = { ok: false, error: { code: "INVALID_INPUT" } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
