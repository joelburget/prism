# Checkpoint 2: leased workers and fenced recovery

Extend the completed **Durable workflow recovery** program. `PREVIOUS.md` contains
the full checkpoint-one contract. Every old request/response and error must remain
unchanged. This follow-up is revealed only after that implementation is frozen.

## Requested change

Support multiple workers with expiring leases, independently delayed service calls
and response commits, worker crashes, renewals, and recovery by another worker.
Keep the existing dependency scheduler, retry policy, cancellation and idempotent
mock service. Add fencing so a stale owner cannot overwrite a replacement's state.
This is a deterministic simulator, not real threads, networking or filesystem I/O.

## Protocol and input modes

Use the same JSON envelope and `task:"workflow-recovery"`. Without a `workers`
field, use the complete previous contract, including its command vocabulary and
exact response shape. With `workers`, use the new mode described here. Its input
fields are required `steps`, `commands`, `workers`, optional `max_attempts` (3),
`retry_delay` (2), `lease_duration` (5), and no others. Step definitions and graph
validation/error precedence are unchanged. `workers` is a nonempty array of
unique IDs matching the old run-ID grammar. All workers initially are up.
Lease duration is an integer 1..1,000,000. At most 100 workers, 200 steps, 100 runs
and 2,000 commands are used; only exceeding the command bound is tested as an
input error. All numeric values reject JSON booleans.

Validate every command's shape and scalar constraints before executing any, then
validate the graph as before. Unknown/missing/extra fields, invalid worker arrays,
invalid IDs, and malformed commands are `INVALID_INPUT`. Runtime errors stop
execution with the old top-level error envelope; no partial result is returned.
A valid result is `{"ok":true,"result":{"results":[VALUE,...],"final":FINAL}}`.
Each command produces one VALUE. Earlier observation values are immutable.

## Durable state and leases

Time starts at 0 and has the old signed-32-bit upper bound. Workers have separate
up/down flags. Run creation order and step definition order remain scheduler order.
A running step has one durable lease `{worker:ID,ticket:N,expires:T}`. Other steps
have `lease:null`. A lease is live exactly when `now < expires`; equality is already
expired. An expired lease remains visible until replaced or cleared. Time passing
alone changes no step/run status and performs no service calls.

Tickets start at 1, increase globally on successful claims, and are never reused.
They identify a particular lease acquisition, not a logical action or retry.
A new pending attempt increments the durable attempt number. Reclaiming a running
attempt uses a new ticket but the **same attempt number**. Every service call uses
the structured logical key `[run_id,step_id]`, stable across tickets and retries.
A worker may hold at most one live lease; its expired tickets do not make it busy.

The simulator retains ticket metadata and service responses separately from worker
availability. This models delayed transport messages: a crash does not erase a
response already produced by the mock. No command guesses effects from the audit;
reconciliation makes a real, audited mock lookup. The mock's effects, execute
failure counters and calls survive all worker crashes.

## Commands and results

Objects have exactly the indicated fields. Worker/run IDs follow the old grammar;
ticket IDs are integers 1..2,147,483,647. Runtime worker validation first checks
existence (`UNKNOWN_WORKER`), then availability (`WORKER_DOWN` where required),
then ticket existence (`UNKNOWN_TICKET`) and ticket ownership (`WRONG_WORKER`).
Ticket ownership means its original worker, even after another worker reclaims.

| Command | Result and behavior |
| --- | --- |
| `{"op":"start","run":"r"}` | null; create the old initial run/step states with null leases. `DUPLICATE_RUN` still applies. No worker is required. |
| `{"op":"advance","by":N}` | null; old validation and `TIME_OVERFLOW` rules. Works with all workers down. |
| `{"op":"observe"}` | Snapshot as described below; no effects. |
| `{"op":"crash","worker":"w"}` | null; mark an up worker down, retaining leases and transport messages. Already down is `WORKER_DOWN`. |
| `{"op":"restart","worker":"w"}` | null; mark a down worker up. Already up is `WORKER_UP`. No recovery happens automatically. |
| `{"op":"claim","worker":"w"}` | `{"ticket":N}` or `{"ticket":null}`; requires up. A live lease owned by w is `WORKER_BUSY`. Select work as below. |
| `{"op":"renew","worker":"w","ticket":N}` | `{"renewed":BOOL}`; requires up and ticket ownership. If current and live, set expires to now + lease_duration, else return false. No new ticket/attempt or call. Overflow is `TIME_OVERFLOW`. |
| `{"op":"call","worker":"w","ticket":N}` | Service receipt or `{"outcome":"stale"}`; requires up and ownership. Rules below. |
| `{"op":"deliver","ticket":N}` | `{"committed":BOOL}`; applies a saved response only under the fencing rules below. Unknown ticket is `UNKNOWN_TICKET`. |
| `{"op":"cancel","run":"r"}` | null; `UNKNOWN_RUN` if absent, otherwise cancellation rules below. |

Old `tick` and checkpoint fields are not accepted in leased mode. Splitting claim,
call and deliver replaces those checkpoints and exposes more interleavings.

### Claim and scheduling

First choose the earliest running step with an expired lease, scanning runs in
creation order and steps in definition order, including cancelling/failing runs.
Otherwise choose the first ready pending step of an active run using the old
creation/definition order, successful dependencies, and ready_at <= now.
Unexpired running steps do not prevent other independent steps from starting.
If no work is eligible, return null ticket and consume no ID. Otherwise persist a
new lease expiring at now + lease_duration (overflow is `TIME_OVERFLOW`) and return
its ticket. Starting pending work changes it to running and increments attempts;
recovery of running work does neither. The ticket captures call kind `execute`
for an active run or `lookup` for a cancelling/failing run.

### Call, delay and fencing

Call requires that the ticket is the step's current, live lease. If not, return
`{"outcome":"stale"}` with no service call. On the first live call, atomically invoke
the mock and save its response, returning `{"kind":"execute","outcome":"applied"}`
(or replayed/transient), or `{"kind":"lookup","outcome":"found"}` (or missing).
A repeated call on that same live ticket returns its saved receipt without another
audit entry or effect. The old mock's failure-count and idempotency rules apply.

Deliver commits only if the ticket has an undelivered saved response, is still
current and live, and its owner is up. Otherwise it returns false and changes
nothing. In particular, a response produced before expiration may not commit at
or after expiration. A response that cannot commit while its worker is down may
commit after restart if its lease is still live. Successful delivery returns true,
clears the step's lease, and marks the response delivered. Duplicate delivery is
false. A stale success or transient response cannot change attempts, retry time,
terminal status, or any replacement worker's lease.

An execute success marks the step succeeded. A transient schedules a retry at
**delivery time** + retry_delay if attempts < max_attempts, with the old overflow
rule. Otherwise it marks the step failed and begins failure draining below.
ready_at otherwise retains its old value. A fully successful active run becomes
succeeded. No dependencies are released by call alone; delivery is required.

### Cancellation and failure draining

Cancellation of succeeded/failed/cancelled/failing runs is a no-op, including its
flag. Cancellation of an active/cancelling run sets cancel_requested true, cancels
pending steps, and sets run status cancelling while running steps remain, otherwise
cancelled. Repeated cancellation in a cancelling run is a no-op: it must not revoke
newly acquired lookup leases.

On the **first** cancellation, expire every running lease immediately by setting
expires to now. Keep ticket/worker visible. Any earlier execute response is now
stale; recovery claims fresh lookup tickets. A lookup found marks its step
succeeded; missing marks it cancelled. When no running steps remain the run becomes
cancelled, even when every step succeeded. Existing effects are never undone.

On committed retry exhaustion in an active run, mark pending steps blocked. If
other running steps remain, enter the new status `failing` and immediately expire
all their leases; otherwise enter failed. Failing work recovers with lookup only:
found marks succeeded, missing marks blocked. After all running work is reconciled,
enter failed. This prevents one failed branch from starting further effects while
preserving effects already produced by other branches. `failing` cannot be changed
to cancelling. Terminal runs contain no running steps and execute no new work.

## Observations and audit

An observation contains exactly `now`, `workers`, `runs`. Workers are objects
`{"id":"w","up":true}` in definition order. Runs keep the old shape and order:
`id,status,cancel_requested,steps`. Steps keep `id,status,attempts,ready_at` and
add `lease` (the object above or null). FINAL adds `calls` and `effects` to this
snapshot. Effects have the old `{key,amount}` shape, in first-application order.
Every actual mock invocation appends exactly one call:

```json
{"worker":"a","ticket":1,"kind":"execute","key":["r","s"],"attempt":1,"outcome":"applied"}
```

Lookup uses found/missing. Retries/reclaims may add calls but at most one effect
exists per logical key. Stale/repeated calls, renewals and delivery add no audit
entries. Runtime error envelopes omit snapshots/audits, as in checkpoint one.

## Example and review questions

Claim ticket 1 on worker a, call it (effect applied), advance exactly one lease
interval, then claim the same running step on b as ticket 2. Delivering ticket 1
returns false. Calling ticket 2 returns replayed with the same attempt number;
delivering it succeeds. The audit has two calls and only one effect.

Every previous public case is a baseline regression; previous held-out cases stay
private. New public cases illustrate the split protocol. Private interleavings
exercise the same published semantics. Run `python3 run.py run --task
workflow-recovery --command './starter/run.sh'`.

Review: Is lease ownership checked at commit as well as call? Are action identity,
attempt number and acquisition ticket distinct? Can cancellation or failure lose
an already applied effect? Can repeated cancellation strand a lookup? Are delivery
and observation values isolated from later state? No threads or simulated sleeps
are required; keep semantic correctness separate from machine timing.
