# Private checkpoint-two coverage

All checkpoint-one held-out cases remain baseline regressions. New cases:

- **cp2-stale-transient-after-success**: A stale transient must not reopen a replacement success or schedule a retry.
- **cp2-uncommitted-transients**: Recovering lost transient responses consumes calls, not new attempt numbers.
- **cp2-renew-at-boundary**: Renewal at exact expiration fails and cannot prevent reclamation.
- **cp2-repeated-cancel-during-lookup**: Repeated cancellation must not expire a newly acquired lookup lease.
- **cp2-reclaimed-lookup**: A lookup response can itself be lost to expiry and recovered without executing.
- **cp2-cancel-two-running**: Mixed found/missing reconciliation drains every running branch and cancels pending children.
- **cp2-failure-drain-missing**: Failure draining blocks an unexecuted sibling and cancellation cannot change failing to cancelling.
- **cp2-failure-other-run**: Draining a failed run does not prevent independent runs from continuing.
- **cp2-recovery-priority**: Expired running work precedes pending work across run boundaries.
- **cp2-dependency-not-call**: An effect alone cannot release a dependent step.
- **cp2-same-worker-reclaim**: A worker can reclaim its own expired lease with a new ticket and unchanged attempt.
- **cp2-down-delivery-recovery**: A response while down cannot commit; after expiry only the replacement may commit.
- **cp2-late-transient-preserves-deadline**: Old transient delivery cannot reset a newer committed retry deadline.
- **cp2-renewal-does-not-unexpire**: Time after a successful renewal crosses the new deadline, not the original one.
- **cp2-unknown-ticket**: Unknown delivery tickets are errors.
- **cp2-wrong-owner**: Ticket ownership remains its original worker even while current.
- **cp2-down-error-precedence**: Down-worker validation precedes unknown-ticket lookup.
- **cp2-command-prevalidation**: A malformed later command wins over an earlier runtime error.
- **cp2-claim-overflow**: Acquiring a lease must check deadline arithmetic.
- **cp2-renew-overflow**: Renewal deadline overflow is an error even with a currently live lease.
- **cp2-retry-overflow**: A committed transient validates retry deadline overflow.
- **cp2-uncalled-delivery**: Delivery without a saved response is harmless and leaves the lease usable.
- **cp2-structured-keys**: Different run/step tuples cannot collide through delimiter concatenation.
- **cp2-generated-812**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-947**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-1203**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-2049**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-4097**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-6001**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-7019**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
- **cp2-generated-9011**: Seeded long interleaving combines recovery, renewal, duplicate/stale messages, retries and terminal draining.
