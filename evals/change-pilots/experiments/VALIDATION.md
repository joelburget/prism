# Experiment infrastructure validation

Initial infrastructure checks on 2026-09-11 used no authenticated model inference.
The subsequent live subscription preflight is described below. These checks
validate infrastructure, not model performance.

- Public maintenance suite: 145 tests discovered, 141 passed and four opt-in Docker
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

Native image: `sha256:9a294e07d162da1c01a8ba3ddcfed8e617c45b748d19eb9c7b672c6d51da9dd6`,
containing Codex 0.154.0 and Claude Code 2.1.257. Both provider containers passed
20 Docker configuration checks each; OpenAI passed 11 and Anthropic 13 network
checks. All eight model configurations passed real-CLI offline inventory and
MCP canary execution through the separate task container. Codex exposes execute
and three empty resource helpers; Claude exposes execute only. Medium effort was
preserved for every model except Haiku, which has no configured effort.

The public native-check command also passed end to end for Luna. Tests cover relay
framing and IDs, builtin-tool leakage in both ordinary and Responses Lite requests,
authentication status redaction, broken relays, blocked stdin deadlines and output
limits. See [machine-readable verification evidence](NATIVE_VERIFICATION.json) and
[reproduction/login instructions](NATIVE.md).

The isolated Codex container recognizes ChatGPT authentication after the documented
headless cache copy. The user subsequently authenticated the isolated Claude
container with Max; the host Mac keychain was not exported.

## Live subscription preflight

All eight selected models completed a small live probe: one execute-tool canary
in a separate task container, followed by `DONE`. These probes used the official
subscription clients, with no API keys or fallback route. They are separate from
the 24 scored calibration cells and used no task solutions or acceptance fixtures.
Claude's assistant metadata reported the requested model IDs. Codex accepted each
explicit model selection but did not expose independent server model identity in
its event stream. Remaining quota and actual subscription invoices were not checked.

Still to validate through calibration: live provider schemas/model availability,
real tool-use behavior, effective model settings, empirical cost/time distributions,
and whether the selected budgets yield useful task difficulty. Subscription-native
batch execution now uses `experiments.native_batch`, with source/score/review capture,
strict input and image checks, and no automatic retries or model fallback.
