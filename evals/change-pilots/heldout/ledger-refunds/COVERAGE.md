# Held-out ledger coverage

Evaluator-only additions to the published `ledger-refunds/PROBLEM.md` contract. These cases add interactions and boundary coverage, not new requirements. Keep this directory and its history outside candidate-agent exports.

There are 23 cases: 5 baseline and 18 extension. The explicit expected outputs are language-independent and use the existing shared-runner schema. No starter or complete reference implementation is included.

| Case | Phase | Public coverage gap | Plausible flawed generalization caught |
| --- | --- | --- | --- |
| `heldout-inclusive-operation-limit` | baseline | Public rejects 501 operations but does not accept exactly 500 or exercise large event IDs. | Off-by-one sequence limit, tiny event tables, or treating period order as event order. |
| `heldout-payment-allocation-limit-atomic` | baseline | Public has small allocation arrays and no inclusive 100-entry boundary. | Truncating oversized batches, validating after partial mutation, or rejecting 100 entries. |
| `heldout-ascii-identifiers-and-deep-retry-equality` | baseline | Public uses conventional identifiers and only tests outer object field-order equality. | Natural-sort reports, stricter-than-specified ID grammar, or serialized-string retry comparison. |
| `heldout-last-period-and-close-validation` | baseline | Public does not close period 1200 or replay a maximum-period invoice afterward. | Assuming there is always another writable period or treating malformed close retries as conflicts. |
| `heldout-failed-key-can-change-operation-kind` | baseline | Public retries corrected failures within one operation kind. | Reserving rejected keys or numbering events by operation position. |
| `heldout-restore-capacity-with-active-sibling-refund` | extension | Public covers per-payment restoration and multi-invoice refunds separately. | Resetting the whole payment capacity or restoring every allocation by the same total. |
| `heldout-overlapping-payment-allocation-grid` | extension | Public multi-invoice integration uses fewer overlapping payment/refund relationships. | Pooling contributions by invoice or payment without indexing both. |
| `heldout-refund-replacement-to-unblock-correction` | extension | Public unblocks refund reversal by reversing the replacement payment wholesale. | Remembering a failed reversal as permanently blocked or allowing reversal after only partial headroom returns. |
| `heldout-backdated-corrections-and-later-invoice` | extension | Public already has a backdated refund after reversal, but not a fully decreasing invoice/payment/refund/reversal period chain plus a later-created invoice excluded from earlier prefixes. | Sorting replay by posting period or building historical reports from current invoice membership. |
| `heldout-old-close-replay-cannot-reopen-period` | extension | Public retries only the currently latest close. | Reapplying an old close payload and lowering closed_through during idempotent replay. |
| `heldout-replay-after-complete-correction-chain` | extension | Public replay-after-reversal cases stop before a replacement payment and complete correction chain coexist. | Rehydrating canceled allocations from cached requests or treating a reused invoice balance as original-payment capacity. |
| `heldout-refund-retry-keeps-original-allocation-order` | extension | Public allocation ordering tests only baseline payment and not canceled refund retries. | Canonicalizing array order or comparing nested JSON text instead of structural objects. |
| `heldout-extension-global-key-and-validation-precedence` | extension | Public global-key fixture only crosses invoice and payment kinds. | Using per-command idempotency maps or performing key lookup before complete shape validation. |
| `heldout-failed-extension-key-reused-across-kinds` | extension | Public reuses rejected extension keys only within the same command kind. | Caching domain failures or reserving keys before a transaction commits. |
| `heldout-future-reference-eventually-becomes-valid` | extension | Public has unknown references but does not later create and reuse the exact formerly missing event ID. | Reserving event IDs on rejection, caching UNKNOWN_EVENT, or confusing input positions with event IDs. |
| `heldout-reversed-target-before-allocation-errors` | extension | Public reversed-target and allocation-precedence cases are separate. | Validating allocation ownership before target liveness or checking liveness before closed-period rules. |
| `heldout-malformed-refund-retries-before-idempotency` | extension | Public shape validation uses fresh extension keys, and its malformed successful-key precedence example is an invoice. | Skipping nested validation for known keys or validating only the first allocation. |
| `heldout-large-aggregate-refund-and-reversal` | extension | Public large-total fixture exercises payments only. | Narrow refund accumulator, signed overflow on inverse events, or clamping invoice-wide refund totals. |
| `heldout-refund-allocation-limit-and-whole-reversal` | extension | Public refund batches are small and do not exercise batch-size validation coupled to reversal. | Sharing an unbounded refund parser, truncating batches, or reversing only part of a large allocation list. |
| `heldout-many-canceled-refunds-retain-retry-tombstones` | extension | Public only has short refund/reversal chains and no replay of multiple old refunds in reverse order. | Keeping only the latest refund or reversal per payment, or reactivating canceled refunds during replay. |
| `heldout-invoice-identity-survives-money-return` | extension | Public duplicate-invoice tests occur before any money-return workflow. | Deleting zero-net-payment invoices or interpreting full refund as invoice cancellation. |
| `heldout-equal-money-effects-are-different-payloads` | extension | Public key conflicts vary amounts or reverse targets but do not compare identical refund allocations against distinct payments. | Deduplicating by net financial effect or omitting period/payment from the request fingerprint. |
| `heldout-nonmonotonic-historical-projections` | extension | Public historical queries mostly enumerate one chain in ascending prefix order. | Mutating a shared historical projection, assuming as_of increases, or letting later close/reversal state leak into cached reports. |

## Validation

Expected outcomes were authored explicitly. Validation independently reconstructs accepted event prefixes using signed payment/refund effects, checks report arithmetic, exact audit prefixes, dense receipts, retry identity, accepted refund capacity, and reversal eligibility. Error decisions are checked separately against the published shape/idempotency/period/domain precedence. This validates the fixtures; it does not establish that the cases exhaust the specification.

All 23 retained cases passed the existing corpus receipt/audit/report projection checks. A
separate, temporary contract check agreed with the accepted writes, replays,
domain/validation errors, and reads. No validation discrepancies remain.

The long boundary fixtures exercise contractual limits, not execution-time performance. No requirements for concurrency, credit notes, multi-currency accounting, or process persistence are introduced.
