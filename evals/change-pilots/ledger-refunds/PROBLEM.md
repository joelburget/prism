# Pilot 13: partial refunds and reversals in an invoice ledger

## Change request

Extend an existing invoice ledger with partial, allocated refunds and compensating
reversals. The existing service issues invoices, applies payments, closes accounting
periods, deduplicates requests, and supplies historical reports and an audit log.
Those behaviors must continue to work. New operations must update invoice balances,
cash, reconciliation totals, historical reports, and the audit trail consistently.

A refund here means **returning money previously applied to an invoice**: it reduces
net paid and reopens the receivable. It does not cancel the sale or reduce the invoice
total. Credit notes, taxes, multiple currencies, interest, persistence across process
invocations, and concurrency are outside this pilot. A reversal corrects a recorded
payment or refund with a new compensating event; it never edits the old event.

This is an extension task, not an invitation to replace the starter with a different
program. Each evaluation run will receive one starter in its assigned language.
Prism and the comparison language run in separate, fresh agent sessions with no
access to each other's code or run artifacts, as described in the parent README.
Preserve the supplied starter's public baseline behavior while extending its domain model.
No starter or complete reference implementation is included in this specification.

## Language-independent execution contract

Read one JSON request from stdin, write one JSON response to stdout, and exit zero.
Diagnostics belong on stderr. The shared runner starts a fresh process for each case.

```json
{"protocol_version":1,"task":"ledger-refunds","input":{"operations":[{"op":"report"}]}}
```

The input contains exactly `operations`, an array of 0–500 operations. Each request
starts with an empty ledger, no events, and `closed_through = 0`. Apply operations in
array order. **An operation error is a result, not a request failure**: preserve state
atomically and continue to the next operation. A successful envelope is:

```json
{"ok":true,"result":{"results":[{"ok":true,"result":{"closed_through":0,"invoices":[],"cash":0,"outstanding":0}}]}}
```

There is one result per operation. Operation failures are exactly
`{"ok":false,"error":{"code":"ERROR_CODE"}}`; successful results are
`{"ok":true,"result":VALUE}`. Malformed task input (missing/extra input fields,
non-array operations, or more than 500 operations) instead produces the top-level
`{"ok":false,"error":{"code":"INVALID_INPUT"}}`. The shared protocol envelope is
always valid in these fixtures. No stack traces, messages, or extra response fields.

All numbers are JSON integers, never booleans or floating-point numbers. Monetary
amounts are minor units of one unnamed currency. Individual amounts are between 1
and 1,000,000,000 inclusive. Accounting periods are integers 1–1200. IDs and request
keys match `[a-z][a-z0-9-]{0,39}`. All fields shown for an operation are required,
except `as_of`; unspecified extra fields are invalid. An unrecognized operation,
invalid object shape, invalid scalar, empty allocation array, duplicate invoice in
an allocation array, or invalid `as_of` type/range is `INVALID_OPERATION`.

At most 500 operations and bounded input amounts keep sums exactly representable by
64-bit integers and JavaScript numbers. Each allocation array has 1–100 entries,
each exactly `{"invoice":ID,"amount":AMOUNT}`. Array order need not be sorted.

## Baseline operations that must remain compatible

| Operation | Fields besides `op` | Behavior |
| --- | --- | --- |
| `invoice` | `key`, `period`, `invoice`, `total` | Issue a unique invoice with a positive total. |
| `payment` | `key`, `period`, `allocations` | Apply one payment to one or more invoices. Its amount is the sum of its allocations. Each allocation must fit that invoice's current outstanding balance. |
| `close` | `key`, `through` | Set `closed_through` to `through` (period 1–1200). It must strictly increase. Financial events already recorded in later periods do not prevent closing an earlier period. |
| `report` | optional `as_of` | Return the report described below, considering an event prefix. |
| `audit` | optional `as_of` | Return the immutable event prefix described below. |

Each successful mutation, including `close`, appends one event with the next integer
ID, starting at 1. It returns `{"event_id":ID,"replayed":false}`. Reads and rejected
operations consume no event IDs. Invoice totals never change. Invoice IDs are unique
for the entire request, including historical events.

Financial writes (`invoice`, `payment`, `refund`, `reverse`) require `period >
closed_through`. Backdated writes in any still-open period are allowed: periods are
posting labels, not a requirement that event IDs follow calendar order. Reads see
events by ID, not by period. `close` is administrative and has no monetary effect.

## Extension operations

### Refund an allocated payment

```json
{"op":"refund","key":"refund-one","period":2,"payment":2,"allocations":[{"invoice":"inv-a","amount":30}]}
```

`payment` must name an existing, unreversed `payment` event. Each refund allocation
must name an invoice in that payment's original allocations. It may consume at most
the original allocation minus **active** refunds against that same payment and
invoice. Refunds belonging to other payments do not use this limit. A refund may
cover a subset of the original allocations; it may be partial or exhaust them.

For every allocation, decrease the invoice's net paid by the amount, increase its
active refunded total by the amount, and decrease cash by the amount. Preserve the
original invoice total. Multiple refunds are allowed. Validate all allocations
before appending the one refund event: a failing allocation rolls back the entire
operation, including its request-key reservation.

### Reverse a payment or refund

```json
{"op":"reverse","key":"correction-one","period":3,"target":3}
```

`target` must name a payment or refund event that has not already been reversed.
Append a new event with the inverse monetary effect of the whole target; partial
reversals are not supported.

* A payment can be reversed only when it has no active refunds. Remove all its
  allocations from net paid and cash. Earlier refunds that were themselves reversed
  do not block this operation.
* Reversing a refund restores its allocations to net paid and cash and removes them
  from active refunded totals. Reject if **any** restored allocation would make its
  invoice's net paid exceed its total (another payment may have filled that gap).
  Successful reversal restores the original payment's refundable capacity.
* A reversal event cannot itself be reversed. No event is deleted or modified.
* Use the reversal's own open posting period; the target's period may be closed.
  A closed period prevents new entries into that period, not corrections in a later
  period. Historical reports taken before the correction remain unchanged.

An event is active if it is a payment or refund and no successful reversal targeting
it occurs in the report's event prefix. There is no general-purpose mutable balance
override operation.

## Idempotency and atomicity

All five mutation kinds use one global request-key namespace. For an already
successful key, the same complete operation payload returns the original event ID
with `replayed:true`, even after closing its period or reversing that event. It
does not append an event or perform current-state validation. A different payload
using that key is `KEY_CONFLICT`.

Payload equality ignores JSON object field order but compares every field and
array order, including allocation order. Do not normalize allocation order before
comparing retry payloads. Reads have no key. An unsuccessful operation does not
reserve its key: a corrected request can later use it.

Each accepted operation is atomic across allocations, balances, reversal state,
audit history, sequence ID, and the idempotency map. A failed operation changes none
of them. These requirements apply to baseline operations too.

## Reports and audit history

`as_of` is an optional integer between 0 and the latest event ID inclusive. Omission
means the latest ID. The prefix includes all events with ID at most `as_of`;
`as_of:0` is the empty initial ledger. A nonnegative integer exceeding the latest
ID is `INVALID_AS_OF`; negative, non-integer, or otherwise malformed values are
`INVALID_OPERATION`.

`report` returns exactly:

```json
{"closed_through":0,"invoices":[{"invoice":"inv-a","total":100,"paid":70,"refunded":30,"due":30,"status":"partially_paid"}],"cash":70,"outstanding":30}
```

Include only invoices issued in the prefix, sorted by invoice ID using ASCII
lexicographic order. `paid` is net active payment allocations minus active refunds;
`refunded` is the sum of active refund allocations. `due = total - paid`. Status is
`unpaid` when paid is zero, `paid` when due is zero, and `partially_paid` otherwise.
Repeated payment/refund cycles can make `refunded` exceed the invoice total; do not
cap it at the total or infer refundable capacity from this invoice-wide value.
`cash` is net active payment amounts minus active refund amounts; `outstanding` is
the sum of invoice due. Thus `cash = sum(paid)` and
`cash + outstanding = sum(total)` in every prefix. `closed_through` is the most
recent close value in that prefix, or zero.

`audit` returns `{"events":[{"event_id":1,"operation":ORIGINAL_OPERATION},...]}`,
ascending by event ID, retaining the exact accepted operation payload (JSON object
key order is irrelevant). Include close and reversal events. Retries and failures
never appear. Earlier query results are values at the time of the query; later
operations cannot retroactively change them.

## Error selection

To avoid language-dependent ambiguity, choose errors in this order:

1. Validate operation shape, required fields, scalar bounds and identifiers, and
   allocation structure: `INVALID_OPERATION`.
2. For mutations, apply successful-key retry/conflict handling: `KEY_CONFLICT` for
   a different payload, or return the replay receipt immediately.
3. For financial mutations, check their posting period: `CLOSED_PERIOD`.
4. Apply the operation-specific checks below, in the listed order. Allocation-wide
   checks are passes over the entire array: e.g. any unknown invoice wins over any
   overpayment, regardless of array order.

| Operation | Remaining checks, in precedence order |
| --- | --- |
| `invoice` | Existing invoice ID: `DUPLICATE_INVOICE`. |
| `payment` | Any missing invoice: `UNKNOWN_INVOICE`; any allocation exceeding current due: `OVERPAYMENT`. |
| `close` | `through <= closed_through`: `INVALID_CLOSE`. |
| `refund` | Missing event: `UNKNOWN_EVENT`; event is not a payment: `INVALID_TARGET`; payment already reversed: `TARGET_REVERSED`; any invoice absent from that payment's allocations: `INVALID_ALLOCATION`; any allocation exceeding remaining refundable capacity: `REFUND_EXCEEDS_PAYMENT`. |
| `reverse` | Missing event: `UNKNOWN_EVENT`; target is not a payment or refund: `INVALID_TARGET`; target already reversed: `ALREADY_REVERSED`; payment has active refunds: `REFUNDS_OUTSTANDING`; refund restoration would overpay: `OVERPAYMENT`. |
| `report`, `audit` | Requested prefix exceeds latest event ID: `INVALID_AS_OF`. |

Event references (`payment`, `target`) are positive integers up to 500; an in-range
reference not yet present is `UNKNOWN_EVENT`, including a reference to the event
that would have been created by the current operation. Validate `through` like a
period. Shape validation also applies to malformed retries before idempotency.

## Acceptance and review

`cases.json` contains explicit expected outputs and marks cases `baseline` or
`extension`. Use the same cases through the shared runner for every implementation;
only the command that launches the implementation changes. Baseline cases must pass
on the future starters and on completed solutions. Extension cases exercise new
behavior and its interactions with existing behavior. The public suite is a
deterministic acceptance suite, not exhaustive proof of correctness or a hidden
test set. No full reference implementation is required to run it.

Review the patch for one coherent definition of active events, checked allocation
updates, preservation of append-only history, isolation of historical snapshots,
and idempotency that covers effects and sequence allocation. Review questions
include: Can a retry move money twice? Can reversing one refund restore another
payment's capacity? Can a failed multi-invoice operation leave a partial update?
Can a correction change a previously returned historical balance?
