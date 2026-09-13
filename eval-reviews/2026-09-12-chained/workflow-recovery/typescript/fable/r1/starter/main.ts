import { readFileSync } from "node:fs";
import { LeasedSimulator, parseLeasedWorkflow } from "./leased.ts";
import { DomainError, parseWorkflow, Simulator } from "./workflow.ts";

function isLeased(input: unknown): boolean {
  return (
    input !== null &&
    typeof input === "object" &&
    !Array.isArray(input) &&
    Object.hasOwn(input, "workers")
  );
}
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
  const input = request?.input;
  const result = isLeased(input)
    ? new LeasedSimulator(parseLeasedWorkflow(input)).run()
    : new Simulator(parseWorkflow(input)).run();
  response = { ok: true, result };
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else if (error instanceof SyntaxError)
    response = { ok: false, error: { code: "INVALID_INPUT" } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
