import { readFileSync } from "node:fs";
import { DomainError, parseWorkflow, Simulator } from "./workflow.ts";
import { LeasedSimulator, parseLeasedWorkflow } from "./leased.ts";

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
  response = {
    ok: true,
    result: request?.input !== null && typeof request?.input === "object" &&
      Object.hasOwn(request.input, "workers")
      ? new LeasedSimulator(parseLeasedWorkflow(request.input)).run()
      : new Simulator(parseWorkflow(request?.input)).run(),
  };
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else if (error instanceof SyntaxError)
    response = { ok: false, error: { code: "INVALID_INPUT" } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
