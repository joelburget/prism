import { readFileSync } from "node:fs";
import { DomainError, validate, validateDatabase, validateRow, fields, requireThat } from "./model.ts";
import type { Value } from "./model.ts";
import { Parser } from "./parser.ts";
import { bind } from "./binder.ts";
import { optimize, execute } from "./engine.ts";
import { MaterializedView } from "./views.ts";
import type { TableState } from "./views.ts";
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
  fields(parsed, ["protocol_version", "task", "input"]);
  requireThat(parsed.protocol_version === 1 && parsed.task === "query-null");
  const inputObject = parsed.input;
  requireThat(inputObject !== null && typeof inputObject === "object" && !Array.isArray(inputObject));
  if (Object.hasOwn(inputObject, "commands")) response = commandMode(inputObject);
  else {
    const [database, queries] = validate(parsed);
    const results = queries.map((q) => {
      const plan = bind(new Parser(q.sql).parse(), database);
      return execute(q.optimize ? optimize(plan) : plan);
    });
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

function commandMode(input: unknown): unknown {
  fields(input, ["database", "commands"]);
  requireThat(Array.isArray(input.commands) && input.commands.length <= 2_000);
  const database = validateDatabase(input.database);
  const states = new Map<string, TableState>();
  for (const [name, table] of database) {
    const order = table.rows.map((_, i) => i + 1);
    states.set(name, {
      table, order, positions: new Map(order.map((id, i) => [id, i])),
      rows: new Map(order.map((id, i) => [id, table.rows[i]])), used: new Set(order),
    });
  }
  const views = new Map<string, MaterializedView>();
  let revision = 0;
  const replies: unknown[] = [];
  for (const raw of input.commands as unknown[]) {
    try {
      fields(raw, commandFields(raw), "INVALID_COMMAND");
      const op = raw.op;
      if (op === "create") {
        commandView(raw.view); requireThat(typeof raw.sql === "string" && typeof raw.optimize === "boolean", "INVALID_COMMAND");
        requireThat(!views.has(raw.view), "VIEW_EXISTS");
        const plan = bind(new Parser(raw.sql).parse(), database);
        views.set(raw.view, new MaterializedView(raw.optimize ? optimize(plan) : plan, states));
        replies.push({ ok: true, result: { view: raw.view, revision } });
      } else if (op === "read") {
        commandView(raw.view); const view = views.get(raw.view); requireThat(view, "UNKNOWN_VIEW");
        replies.push({ ok: true, result: { revision, ...view.read() } });
      } else if (op === "drop") {
        commandView(raw.view); requireThat(views.delete(raw.view), "UNKNOWN_VIEW");
        replies.push({ ok: true, result: { dropped: raw.view } });
      } else {
        requireThat(op === "apply" && Array.isArray(raw.changes) && raw.changes.length > 0 && raw.changes.length <= 200, "INVALID_COMMAND");
        const changed = applyBatch(raw.changes, states);
        for (const view of views.values()) view.apply(changed);
        revision++;
        replies.push({ ok: true, result: { revision } });
      }
    } catch (e) {
      if (!(e instanceof DomainError) && !(e instanceof SyntaxError)) throw e;
      replies.push({ ok: false, error: { code: e instanceof DomainError ? e.message : "PARSE_ERROR" } });
    }
  }
  return { ok: true, result: { results: replies } };
}

function commandFields(raw: unknown): string[] {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) throw new DomainError("INVALID_COMMAND");
  const op = (raw as Record<string, unknown>).op;
  if (op === "create") return ["op", "view", "sql", "optimize"];
  if (op === "read" || op === "drop") return ["op", "view"];
  if (op === "apply") return ["op", "changes"];
  throw new DomainError("INVALID_COMMAND");
}
function commandView(name: unknown): asserts name is string {
  requireThat(typeof name === "string" && /^[a-z][a-z0-9-]{0,39}$/.test(name), "INVALID_COMMAND");
}
function changeFields(raw: unknown): string[] {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) throw new DomainError("INVALID_COMMAND");
  const op = (raw as Record<string, unknown>).op;
  if (op === "insert" || op === "update") return ["op", "table", "id", "row"];
  if (op === "delete") return ["op", "table", "id"];
  throw new DomainError("INVALID_COMMAND");
}
function applyBatch(changes: unknown[], states: Map<string, TableState>): Map<string, Set<number>> {
  // Shape validation is deliberately completed before table/row semantics.
  for (const raw of changes) {
    fields(raw, changeFields(raw), "INVALID_COMMAND");
    requireThat(typeof raw.table === "string" && /^[a-z_][a-z0-9_]*$/.test(raw.table), "INVALID_COMMAND");
    requireThat(typeof raw.id === "number" && Number.isInteger(raw.id) && raw.id >= 1 && raw.id <= 2_147_483_647, "INVALID_COMMAND");
    if (raw.op !== "delete") requireThat(Array.isArray(raw.row), "INVALID_COMMAND");
  }
  const privateStates = new Map<string, TableState>();
  for (const [name, s] of states) privateStates.set(name, {
    table: s.table, order: [...s.order], positions: new Map(s.positions), rows: new Map(s.rows), used: new Set(s.used),
  });
  const changed = new Map<string, Set<number>>();
  for (const raw of changes as Record<string, unknown>[]) {
    const state = privateStates.get(raw.table as string);
    requireThat(state, "UNKNOWN_TABLE");
    const id = raw.id as number, op = raw.op;
    if (op !== "delete") validateRow(raw.row, state.table.columns, "INVALID_ROW");
    if (op === "insert") {
      requireThat(!state.used.has(id), "ROW_ID_USED");
      state.used.add(id); state.order.push(id); state.rows.set(id, raw.row as Value[]);
    } else {
      requireThat(state.rows.has(id), "UNKNOWN_ROW");
      if (op === "update") state.rows.set(id, raw.row as Value[]);
      else { state.rows.delete(id); state.order.splice(state.order.indexOf(id), 1); }
    }
    const ids = changed.get(raw.table as string) ?? new Set<number>(); ids.add(id); changed.set(raw.table as string, ids);
  }
  for (const [name, next] of privateStates) {
    const current = states.get(name)!;
    current.order = next.order; current.positions = new Map(next.order.map((id, i) => [id, i]));
    current.rows = next.rows; current.used = next.used;
    current.table.rows = next.order.map((id) => next.rows.get(id)!);
  }
  return changed;
}
