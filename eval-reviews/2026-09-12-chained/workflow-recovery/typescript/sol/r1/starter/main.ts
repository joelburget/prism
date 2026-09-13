import { readFileSync } from "node:fs";
import { DomainError, parseWorkflow, Simulator } from "./workflow.ts";

/** Parse the protocol JSON while retaining its stricter integer and key rules. */
function parseRequest(source: string): unknown {
  let offset = 0;
  const invalid = (): never => { throw new DomainError("INVALID_INPUT"); };
  const whitespace = (): void => {
    while (" \t\r\n".includes(source[offset] ?? "\0")) offset++;
  };
  const string = (): string => {
    if (source[offset] !== '"') invalid();
    const start = offset++;
    while (offset < source.length) {
      const char = source[offset++];
      if (char === '"') {
        try {
          return JSON.parse(source.slice(start, offset)) as string;
        } catch {
          invalid();
        }
      }
      if (char === "\\") offset++;
    }
    return invalid();
  };
  const value = (): unknown => {
    whitespace();
    const char = source[offset];
    if (char === '"') return string();
    if (char === "{") {
      offset++;
      const result: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
      const keys = new Set<string>();
      whitespace();
      if (source[offset] === "}") {
        offset++;
        return result;
      }
      while (true) {
        whitespace();
        const key = string();
        if (keys.has(key)) invalid();
        keys.add(key);
        whitespace();
        if (source[offset++] !== ":") invalid();
        result[key] = value();
        whitespace();
        const separator = source[offset++];
        if (separator === "}") return result;
        if (separator !== ",") invalid();
      }
    }
    if (char === "[") {
      offset++;
      const result: unknown[] = [];
      whitespace();
      if (source[offset] === "]") {
        offset++;
        return result;
      }
      while (true) {
        result.push(value());
        whitespace();
        const separator = source[offset++];
        if (separator === "]") return result;
        if (separator !== ",") invalid();
      }
    }
    for (const [token, parsed] of [
      ["true", true],
      ["false", false],
      ["null", null],
    ] as const) {
      if (source.startsWith(token, offset)) {
        offset += token.length;
        return parsed;
      }
    }
    const match = /^-?(?:0|[1-9][0-9]*)/.exec(source.slice(offset));
    if (!match) return invalid();
    offset += match[0].length;
    return Number(match[0]);
  };

  const result = value();
  whitespace();
  if (offset !== source.length) invalid();
  return result;
}

function inputFromEnvelope(raw: unknown): unknown {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw))
    throw new DomainError("INVALID_INPUT");
  const envelope = raw as Record<string, unknown>;
  if (
    Object.keys(envelope).length !== 3 ||
    !Object.hasOwn(envelope, "protocol_version") ||
    !Object.hasOwn(envelope, "task") ||
    !Object.hasOwn(envelope, "input") ||
    envelope.protocol_version !== 1 ||
    envelope.task !== "workflow-recovery"
  ) throw new DomainError("INVALID_INPUT");
  return envelope.input;
}

let response: unknown;
try {
  const input = inputFromEnvelope(parseRequest(readFileSync(0, "utf8")));
  response = {
    ok: true,
    result: new Simulator(parseWorkflow(input)).run(),
  };
} catch (error) {
  if (error instanceof DomainError)
    response = { ok: false, error: { code: error.code } };
  else throw error;
}
process.stdout.write(JSON.stringify(response) + "\n");
