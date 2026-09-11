# Held-out workflow recovery coverage

Evaluator-only material: exclude this directory from candidate workspaces and public starter exports. All expected behavior is specified by the public `workflow-recovery/PROBLEM.md`; these cases add no requirements.

The 26 fixed cases contain 5 baseline regressions and 21 extension checks. They target interactions absent from the 57-case public corpus. Expectations are explicit states and service-call sequences, authored independently of a runner implementation. This set contains no starter or reference implementation.

| Case | Phase | Gap relative to the public corpus | Plausible faulty generalization caught |
| --- | --- | --- | --- |
| `heldout-staggered-runs-through-forward-join` | baseline | Public multi-run graphs have no dependencies; forward-reference/dependency cases run only one run. | Round-robin runs, dependency-blind definition order, or shared per-step progress across runs. |
| `heldout-creation-deadlines-through-slow-chain` | baseline | Public creation-time test has one step and no delayed dependencies. | Setting ready_at to dependency-completion time or attempt-start time. |
| `heldout-case-sensitive-component-identities` | baseline | Public key-collision test uses lowercase IDs; permitted uppercase and punctuation-only identifiers are untested. | Case-folded keys or overly restrictive identifier parsing. |
| `heldout-graph-uniqueness-precedes-reference-check` | baseline | Public malformed-graph cases isolate each validation failure. | Checking missing references before the specified uniqueness pass. |
| `heldout-late-static-error-precedes-runtime-duplicate` | baseline | Public scalar failures do not follow commands that would fail at runtime. | Validating commands lazily and reporting DUPLICATE_RUN before noticing the boolean advance. |
| `heldout-later-inflight-preempts-older-due-run` | extension | Public in-flight priority covers two steps in one run, not a later run competing with an earlier ready run. | Scanning each run for ready work before globally prioritizing in-flight work. |
| `heldout-cancelling-later-inflight-preempts-due-run` | extension | Public lookup cancellation never competes with another run that is already ready. | Prioritizing active runs over cancelling in-flight runs or executing on cancellation. |
| `heldout-staggered-retries-later-effect-cancelled` | extension | Public retry and cancellation cases do not combine staggered run creation, distinct deadlines, and a later-run applied effect. | Shared retry deadlines, cancellation spilling across runs, or lookup hard-coded to attempt one. |
| `heldout-same-policy-diverges-under-response-loss` | extension | Public multi-run failures both fail; lost-response budget tests have only one run. | Global service-failure counters or charging a new durable attempt for every service call. |
| `heldout-lost-final-attempt-transient-fails-on-recovery` | extension | Public recovery of another transient uses the first attempt with remaining retry capacity. | Resetting attempts at restart, scheduling an extra retry past the budget, or erasing completed branches. |
| `heldout-exhaustion-at-time-bound-does-not-schedule` | extension | Public overflow case only covers retry capacity remaining. | Computing now + retry_delay before checking whether the attempt budget is exhausted. |
| `heldout-retry-deadline-exactly-time-bound` | extension | Public time tests cover overflow but not inclusive-bound retry recovery. | Rejecting equality with the time limit or recomputing the deadline on restart. |
| `heldout-many-uncommitted-failures-one-attempt` | extension | Public max-one recovery loses only one transient response. | Counting recovery calls against the attempt budget or caching transient responses permanently. |
| `heldout-mixed-replay-crash-windows-then-cancel` | extension | Public replay crashes repeat the same checkpoint and do not transition into cancellation. | Adding a call at after_begin, advancing the attempt during replay, or forgetting cancellation after replay. |
| `heldout-repeated-missing-lookups-lost-before-commit` | extension | Public missing-lookup crash occurs before lookup; post-lookup crashes are tested only when an effect is found. | Treating missing as durably cancelled before the after_call checkpoint or changing lookup into execute after restart. |
| `heldout-found-lookup-precall-and-passive-crashes` | extension | Public passive restart does not involve a cancelling run with a service effect. | Implicit reconciliation on restart or losing cancellation flags during an ordinary second crash. |
| `heldout-cancel-second-attempt-before-effect` | extension | Public missing-lookups all reconcile the first attempt. | Resetting attempts or deadlines when cancelling, or executing the eligible retry rather than looking up. |
| `heldout-cancel-second-attempt-repeated-found-lookup` | extension | Public repeated found-lookup uses an immediately successful first attempt. | Reconstructing running attempts from initial defaults or updating deadlines during reconciliation. |
| `heldout-mixed-terminal-run-cancellation-flags` | extension | Public terminal cancellation covers success and failure separately, with no mixed cancelled run or divergent recovery outcome. | Setting every cancellation flag true, sharing terminal state across runs, or revisiting terminal work. |
| `heldout-recovered-diamond-join-effect-cancelled` | extension | Public cancelled join scenario cancels a branch before the join, without a retried ancestor and separately recovered branch. | Cancelling already successful prerequisites or starting a tail after finding the uncertain join effect. |
| `heldout-exhaustion-blocks-already-retrying-sibling` | extension | Public exhausted-run blocking covers unstarted siblings or successful siblings, not pending siblings with attempts/deadlines. | Blocking only steps with attempts zero or leaving a sibling retry eligible in a failed run. |
| `heldout-cancel-distinct-sibling-retry-deadlines` | extension | Public scheduled-retry cancellation is single-step; idle checkpoint tests do not follow multi-step cancellation. | Using one run-level deadline, resetting retained deadlines, or reaching a crash checkpoint for cancelled work. |
| `heldout-down-start-precedes-duplicate-id` | extension | Public process-down precedence covers cancellation but never start with an existing ID. | Checking run duplication before process availability for start. |
| `heldout-ordinary-crash-while-already-down` | extension | Public down-state errors cover tick/cancel; no repeated crash follows an effectful checkpoint. | Treating crash as unconditionally idempotent rather than enforcing PROCESS_DOWN. |
| `heldout-final-down-state-keeps-uncertainty` | extension | Public down snapshots subsequently recover; final results do not remain down with an uncommitted recorded effect. | Automatically recovering or normalizing in-flight state when serializing the final result. |
| `heldout-cancelled-run-id-cannot-be-reused` | extension | Public duplicate-run case reuses a succeeded run ID, not a cancelled/reconciled one. | Deleting cancelled runs from identity bookkeeping or allowing ID reuse when no effect exists. |

## Validation notes

Every successful case has been manually traced command by command, including every tick choice, attempt number, checkpoint, retained deadline, and observation. A separate check verifies all mock-service outcomes against per-key configured failure counts and existing effects, plus ordered effect amounts, run/step output ordering, dependency-success constraints, and terminal-state invariants. These checks inspect authored expectations; they do not generate those expectations or decide the domain protocol for arbitrary candidate inputs.

Particularly discriminating traces:

- `heldout-later-inflight-preempts-older-due-run`: old attempt 1 commits a transient at time 0 (deadline 3); new attempt 1 is begun without a call. At time 3 its recovery must run first, commit its transient, and acquire deadline 6. Old attempt 2 then succeeds; new attempt 2 succeeds at time 6.
- `heldout-same-policy-diverges-under-response-loss`: run one receives transient responses on calls 1 and 2 with durable attempt 1, then succeeds on attempt 2. Run two receives its two transient responses on durable attempts 1 and 2 and fails. The identical policy produces different outcomes solely because of the specified commit boundary.
- `heldout-lost-final-attempt-transient-fails-on-recovery`: a first transient commits attempt 1, an independent branch succeeds, attempt 2 loses transient response 2, and recovery receives transient response 3 on attempt 2 at time 7. Budget exhaustion fails immediately; it does not schedule from time 7 or erase the successful branch.
- `heldout-exhaustion-blocks-already-retrying-sibling`: both independent branches have pending attempt-1 retries at time 3. Definition-order selection executes a's second attempt and fails; b becomes blocked while retaining attempt 1 and deadline 3. The join remains unstarted and blocked.

The suite does not claim exhaustive state-space exploration, hidden security properties, performance coverage, concurrency, or actual operating-system persistence. No implementation conformance result is claimed until candidates are run.
