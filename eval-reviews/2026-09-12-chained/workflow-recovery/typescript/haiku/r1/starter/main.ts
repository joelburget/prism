import { readFileSync } from "node:fs";
import {
  DomainError,
  parseWorkflow,
  parseWorkerWorkflow,
  Simulator,
  WorkerSimulator,
} from "./workflow.ts";

let response: unknown;
try {
  const request = JSON.parse(
    readFileSync(0, "utf8"),
    (_key: string, value: unknown, context?: { source?: string }) => {
      if (
        typeof value === "number" &&
        context?.source &&
        !/^-?(0|[1-9][0-9]*)$/.test(context.source)
      )
        throw new DomainError("INVALID_INPUT");
      return value;
    },
  ) as { input?: unknown };

  const input = request?.input as Record<string, unknown> | undefined;
  if (!input) {
    throw new DomainError("INVALID_INPUT");
  }

  if (Object.hasOwn(input, "workers")) {
    response = {
      ok: true,
      result: new WorkerSimulator(parseWorkerWorkflow(input)).run(),
    };
  } else {
    response = {
      ok: true,
      result: new Simulator(parseWorkflow(input)).run(),
    };
  }
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else if (error instanceof SyntaxError)
    response = { ok: false, error: { code: "INVALID_INPUT" } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
