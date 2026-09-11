# Experiment infrastructure validation

Verified on 2026-09-11. **No authenticated model inference or paid API requests
were made.** These checks validate infrastructure, not model performance.

- Public maintenance suite: 103 tests discovered, 99 passed and four opt-in Docker
  tests skipped in the default invocation. The four Docker tests were separately
  run successfully against the built image.
- Evaluator-only maintenance suite: nine tests passed. The frozen corpus/spec/runner
  manifest still matches; no acceptance cases or starter implementations changed.
- Live Docker acceptance: all nine starters passed 192 public baseline checks,
  plus 45 evaluator-only baseline checks: **237/237**. Each case ran in a fresh
  container after building the frozen source in a separate build container.
- Docker boundary checks covered network/mount/credential exclusion, execution
  after freezing, per-case filesystem reset, timeout cleanup, source-only snapshots,
  invalid artifacts, and submission-build versus infrastructure errors.
- Provider tests used synthetic responses for both continuation protocols,
  opaque reasoning retention, cache/reasoning accounting, tariffs, malformed output,
  time/turn/spending limits and ambiguous-billing stops.
- Controller integration tests used fake models/containers with real plans and
  artifact storage to verify grading, resumption, retained spend, whole-run
  reservations and no retries of interrupted or uncertain-billing runs.
- Review storage/HTTP tests covered immutable records, rebuilding SQLite, blinding,
  explicit post-review reveal, context path restrictions, escaping, and local
  request protection. A Chrome synthetic-data walkthrough successfully opened
  the review, submitted a timed review, and revealed results afterward.

Toolchain image:

```text
prism-change-pilots:0.18.0-py3.14.7-node25.2.1
sha256:d97c8d248720917885ae7394258fc373b417a22207864071ea3e14f5f8d7c742
```

Its pinned compiler source is Prism v0.18.0 commit
`2cfe818bc17d91c2a5fb452901cb40b8e8bee564`. Runtime checks reported Python 3.14.7,
Node 25.2.1, Prism 0.18.0 and TypeScript 5.9.3. Rebuilding with mutable base/apt
repositories may produce a different image; the experiment records its actual ID.

Installed native clients reported Codex 0.154.0 and Claude Code 2.1.257.
Separate read-only authentication checks reported ChatGPT login and claude.ai Max
login respectively. These checks did not establish all eight model entitlements,
remaining subscription quota, or a usable isolated native execution route.

Still to validate through calibration: live provider schemas/model availability,
real tool-use behavior, effective model settings, empirical cost/time distributions,
and whether the selected budgets yield useful task difficulty. Subscription-native
execution remains disabled pending its own client isolation and routing validation.
