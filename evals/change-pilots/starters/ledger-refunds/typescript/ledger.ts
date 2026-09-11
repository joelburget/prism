import { readFileSync } from "node:fs";

// The wire boundary admits only validated operations into the domain.
type Allocation = Readonly<{ invoice: string; amount: number }>;
type IssueInvoice = Readonly<{
  op: "invoice";
  key: string;
  period: number;
  invoice: string;
  total: number;
}>;
type Payment = Readonly<{
  op: "payment";
  key: string;
  period: number;
  allocations: readonly Allocation[];
}>;
type Close = Readonly<{ op: "close"; key: string; through: number }>;
type Mutation = IssueInvoice | Payment | Close;
type ReadOperation = Readonly<{ op: "report" | "audit"; as_of?: number }>;
type Operation = Mutation | ReadOperation;
type Receipt = { event_id: number; replayed: boolean };
type Response =
  | { ok: true; result: unknown }
  | { ok: false; error: { code: string } };

class OperationError extends Error {
  readonly code: string;
  constructor(code = "INVALID_OPERATION") {
    super(code);
    this.code = code;
  }
}
function requireValid(
  condition: unknown,
  code = "INVALID_OPERATION",
): asserts condition {
  if (!condition) throw new OperationError(code);
}
function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function exactFields(
  value: Record<string, unknown>,
  fields: readonly string[],
): boolean {
  return (
    Object.keys(value).length === fields.length &&
    fields.every((key) => Object.hasOwn(value, key))
  );
}
function identifier(value: unknown): value is string {
  return (
    typeof value === "string" && /^[a-z][a-z0-9-]{0,39}(?![\s\S])/.test(value)
  );
}
function integer(
  value: unknown,
  low: number,
  high = Infinity,
): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= low &&
    value <= high
  );
}
function parseOperation(raw: unknown): Operation {
  requireValid(isObject(raw));
  const kind = raw.op;
  if (kind === "report" || kind === "audit") {
    requireValid(exactFields(raw, ["op"]) || exactFields(raw, ["op", "as_of"]));
    requireValid(!Object.hasOwn(raw, "as_of") || integer(raw.as_of, 0));
    return Object.hasOwn(raw, "as_of")
      ? { op: kind, as_of: raw.as_of as number }
      : { op: kind };
  }
  requireValid(kind === "invoice" || kind === "payment" || kind === "close");
  const fields =
    kind === "invoice"
      ? ["op", "key", "period", "invoice", "total"]
      : kind === "payment"
        ? ["op", "key", "period", "allocations"]
        : ["op", "key", "through"];
  requireValid(exactFields(raw, fields) && identifier(raw.key));
  if (kind === "close") {
    requireValid(integer(raw.through, 1, 1200));
    return { op: kind, key: raw.key, through: raw.through };
  }
  requireValid(integer(raw.period, 1, 1200));
  if (kind === "invoice") {
    requireValid(
      identifier(raw.invoice) && integer(raw.total, 1, 1_000_000_000),
    );
    return {
      op: kind,
      key: raw.key,
      period: raw.period,
      invoice: raw.invoice,
      total: raw.total,
    };
  }
  requireValid(
    Array.isArray(raw.allocations) &&
      raw.allocations.length >= 1 &&
      raw.allocations.length <= 100,
  );
  const seen = new Set<string>();
  const allocations = raw.allocations.map((item: unknown): Allocation => {
    requireValid(isObject(item) && exactFields(item, ["invoice", "amount"]));
    requireValid(
      identifier(item.invoice) && integer(item.amount, 1, 1_000_000_000),
    );
    requireValid(!seen.has(item.invoice));
    seen.add(item.invoice);
    return Object.freeze({ invoice: item.invoice, amount: item.amount });
  });
  return {
    op: kind,
    key: raw.key,
    period: raw.period,
    allocations: Object.freeze(allocations),
  };
}

// Recursive object ordering gives payload equality without changing allocation order.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isObject(value))
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value) as string;
}
interface LedgerEvent {
  readonly eventId: number;
  readonly mutation: Mutation;
  readonly payload: Record<string, unknown>;
  readonly fingerprint: string;
}
interface InvoiceBalance {
  total: number;
  paid: number;
}

class Projection {
  closedThrough = 0;
  readonly invoices = new Map<string, InvoiceBalance>();

  apply(mutation: Mutation): void {
    switch (mutation.op) {
      case "invoice":
        this.invoices.set(mutation.invoice, { total: mutation.total, paid: 0 });
        break;
      case "payment":
        for (const allocation of mutation.allocations) {
          this.invoices.get(allocation.invoice)!.paid += allocation.amount;
        }
        break;
      case "close":
        this.closedThrough = mutation.through;
        break;
    }
  }
  report(): unknown {
    const invoices = [...this.invoices.keys()].sort().map((invoice) => {
      const balance = this.invoices.get(invoice)!;
      const due = balance.total - balance.paid;
      return {
        invoice,
        total: balance.total,
        paid: balance.paid,
        refunded: 0,
        due,
        status:
          balance.paid === 0 ? "unpaid" : due === 0 ? "paid" : "partially_paid",
      };
    });
    return {
      closed_through: this.closedThrough,
      invoices,
      cash: invoices.reduce((sum, row) => sum + row.paid, 0),
      outstanding: invoices.reduce((sum, row) => sum + row.due, 0),
    };
  }
}

class Ledger {
  private readonly events: LedgerEvent[] = [];
  private readonly successfulKeys = new Map<string, LedgerEvent>();
  private readonly current = new Projection();

  private validateMutation(mutation: Mutation): void {
    if (mutation.op === "close") {
      requireValid(
        mutation.through > this.current.closedThrough,
        "INVALID_CLOSE",
      );
      return;
    }
    requireValid(mutation.period > this.current.closedThrough, "CLOSED_PERIOD");
    if (mutation.op === "invoice") {
      requireValid(
        !this.current.invoices.has(mutation.invoice),
        "DUPLICATE_INVOICE",
      );
    } else {
      // Validate all references before considering any balance; commit in a later phase.
      requireValid(
        mutation.allocations.every((a) => this.current.invoices.has(a.invoice)),
        "UNKNOWN_INVOICE",
      );
      requireValid(
        mutation.allocations.every((a) => {
          const balance = this.current.invoices.get(a.invoice)!;
          return a.amount <= balance.total - balance.paid;
        }),
        "OVERPAYMENT",
      );
    }
  }
  private read(operation: ReadOperation): unknown {
    const end = operation.as_of ?? this.events.length;
    requireValid(end <= this.events.length, "INVALID_AS_OF");
    const prefix = this.events.slice(0, end);
    if (operation.op === "audit") {
      return {
        events: prefix.map((event) => ({
          event_id: event.eventId,
          operation: structuredClone(event.payload),
        })),
      };
    }
    const snapshot = new Projection();
    for (const event of prefix) snapshot.apply(event.mutation);
    return snapshot.report();
  }
  private mutate(
    mutation: Mutation,
    payload: Record<string, unknown>,
  ): Receipt {
    const fingerprint = canonical(payload);
    const prior = this.successfulKeys.get(mutation.key);
    if (prior) {
      requireValid(prior.fingerprint === fingerprint, "KEY_CONFLICT");
      return { event_id: prior.eventId, replayed: true };
    }
    this.validateMutation(mutation);
    const event: LedgerEvent = Object.freeze({
      eventId: this.events.length + 1,
      mutation: Object.freeze(mutation),
      payload: structuredClone(payload),
      fingerprint,
    });
    this.current.apply(mutation);
    this.events.push(event);
    this.successfulKeys.set(mutation.key, event);
    return { event_id: event.eventId, replayed: false };
  }
  execute(raw: unknown): Response {
    try {
      const operation = parseOperation(raw);
      const result =
        operation.op === "report" || operation.op === "audit"
          ? this.read(operation)
          : this.mutate(operation as Mutation, raw as Record<string, unknown>);
      return { ok: true, result };
    } catch (error) {
      if (error instanceof OperationError)
        return { ok: false, error: { code: error.code } };
      throw error;
    }
  }
}

function run(request: unknown): Response {
  const input = isObject(request) ? request.input : undefined;
  if (
    !isObject(input) ||
    !exactFields(input, ["operations"]) ||
    !Array.isArray(input.operations) ||
    input.operations.length > 500
  ) {
    return { ok: false, error: { code: "INVALID_INPUT" } };
  }
  const ledger = new Ledger();
  return {
    ok: true,
    result: { results: input.operations.map((op) => ledger.execute(op)) },
  };
}

// Preserve the protocol's integer-token distinction (JSON.parse alone turns 1.0 into 1).
const source = readFileSync(0, "utf8").split("\n", 1)[0]!;
const request: unknown = JSON.parse(
  source,
  (_key: string, value: unknown, context?: { source?: string }) => {
    if (typeof value !== "number" || !context?.source) return value;
    if (/[.eE]/.test(context.source)) return NaN;
    // Out-of-range integer tokens still need the correct as_of error. Every valid
    // stored numeric field is bounded, so this sentinel cannot enter an event.
    return Number.isFinite(value) ? value : Math.sign(value) * Number.MAX_VALUE;
  },
);
process.stdout.write(`${JSON.stringify(run(request))}\n`);
