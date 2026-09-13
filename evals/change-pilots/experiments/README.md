# Repeated programming-agent experiments

Current runtime: Prism **0.22.0**. See [migration checks](VALIDATION-0.22.md)
and the [full tutorial/context profile](context/README.md) for new cohorts.

This evaluator-side package supplies a shared OpenAI/Anthropic API agent loop,
isolated Docker workspaces, frozen experiment plans, cost scenarios, source and
trace capture, external grading, and a local human-review interface. It is never
included in implementation-agent exports. No paid rollouts were run while building it.

Read [the cost estimate](COST_ESTIMATE.md) before scheduling paid work. All eight
model classes are in [models.json](models.json); catalog inclusion is not evidence
that a particular API account can access the model.

## Current readiness

- The API path is implemented and tested with fake provider responses, real Docker
  tool execution, and real starter acceptance cases. Live API authentication,
  account access, model-specific behavior, and empirical rollout cost still need calibration.
- Native clients now have separate Docker containers and a working MCP bridge to
  the task container. [Native setup](NATIVE.md) describes the reproducible offline
  checks and subscription login. `experiments.native_batch` runs frozen subscription
  experiments with the same grading and review artifacts, under a separate harness
  label. Remaining subscription quota and actual charges are not inferred from
  the CLI's API-equivalent dollar estimates.
- Runs are deliberately serial within an experiment. The controller uses a file
  lock, resumes by skipping finished runs, and never silently retries an interrupted
  or ambiguously billed request. There is no automatic concurrent scheduler yet.

## Toolchain and isolation

From the repository root:

```sh
python3 evals/change-pilots/experiments/build_image.py
```

The build script exports only allowlisted compiler files from pinned Prism 0.22.0
commit `b643c4acfd371ba37d13fdedf0742154e5b69806`; neither Git nor evaluation files
enter its build context. It installs Python 3.14.7, Node 25.2.1, TypeScript 5.9.3,
Node types 25.0.3, LLVM 22, and offline Prism documentation. Package/base-image tags
are not fully reproducible at rebuild time; each experiment records and reuses the
resolved immutable Docker image ID.

The host controller alone calls the providers and reads credentials. Each agent
gets one task/language export in `/work` inside an unprivileged, network-disabled
container with no host mounts or Docker socket. The same `execute` tool is exposed
to both providers. Shell calls have bounded time/output; an overflow or timeout
stops the container and terminates that rollout's tool activity. Subprocesses cannot
continue modifying a submitted source snapshot after freezing.

Only regular UTF-8 source/config/test files under `starter/` are submitted. Allowed
suffixes are `.pr`, `.py`, `.ts`, `.json`, `.md`, `.sh`, `.toml`, `.txt`, `.lock`, plus
`.gitignore`; dependency/build/cache directories are excluded. The prompt states
this artifact contract. Snapshots reject links, special files, excessive file sizes,
and missing launchers. The evaluator builds the frozen source in a separate
container, then creates a **fresh container for every case**, sending only that
case's input over stdin. Expected results and future cases never enter it.

Docker and its image are trusted infrastructure, not a VM security boundary.
The runtime has CPU, memory, process, output and per-file limits, but no aggregate
writable-layer disk quota. Run experiments on a machine with adequate Docker disk
space. The held-out fixtures exist in GitHub history; network/filesystem exclusion
prevents retrieval during these runs, but does not prove training-data secrecy.

## Estimate and plan without spending

```sh
# Eight models × three tasks × three languages × three repetitions = 216 runs.
python3 evals/change-pilots/experiments/control.py estimate

# First calibration: all eight models, one task, all three languages = 24 runs.
python3 evals/change-pilots/experiments/control.py plan \
  --results /path/outside/git/prism-calibration \
  --task ledger-refunds --repetitions 1

# Full pilot, saved separately.
python3 evals/change-pilots/experiments/control.py plan \
  --results /path/outside/git/prism-pilot --repetitions 3
```

`--model` accepts `luna`, `terra`, `sol`, `astra`, `haiku`, `sonnet`, `opus`, or
`fable`; repeat it to select several. Repeat `--task` and `--language` similarly.
Planning records randomized run order, independent repetition IDs, exact prompts,
model settings/prices, source/corpus/harness hashes, and budgets. A changed frozen
input requires a new plan. The ordering seed is not a deterministic model seed.

Default per-run limits: 100 model calls, 30 minutes of agent wall time, 8,192
output tokens per response (including thinking), and $20 estimated API spend.
There is also a conservative 260,000-token input guard estimated from serialized
UTF-8 request bytes plus overhead; it is not a tokenizer measurement. There is no
automatic context compaction. The input guard keeps the shipped configuration below
the OpenAI long-context pricing threshold and may stop a long conversation early.

OpenAI uses `medium` reasoning. Newer Claude models use adaptive thinking with
`medium` effort; Haiku uses no explicit extended thinking. These settings are recorded,
not asserted to represent equal compute. They can be changed in a versioned model
catalog before planning a separate experiment.

## Run and resume

Set `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in the **controller environment**.
Do not put credentials in prompts, source files, plan JSON, or the repository.
The catalog contains fixed official endpoints and the transport rejects redirects.

```sh
# Example only: executing this authorizes up to the specified cumulative estimate.
python3 evals/change-pilots/experiments/control.py run \
  --results /path/outside/git/prism-calibration --budget-usd 150 --limit 1

# Resume remaining runs under the same cumulative cap.
python3 evals/change-pilots/experiments/control.py run \
  --results /path/outside/git/prism-calibration --budget-usd 150

python3 evals/change-pilots/experiments/control.py status \
  --results /path/outside/git/prism-calibration
```

The controller reserves the plan's entire per-run allowance before starting the
next run. Within a run it estimates the next request conservatively without cache
discounts and reserves its full output allowance. Actual returned usage separates
uncached input, cache reads/writes, output, and reasoning; reasoning is a subset of
output, not another charge. Prices are estimates rather than account billing receipts.
Unknown billing halts the campaign instead of freeing the reservation for another run.
Interrupted/unknown-billing records must be reconciled before resumption; automatic
reconciliation is not implemented. There is no provider/model fallback.

Baseline regressions and extension outcomes are reported separately for public and
held-out corpora. A failed submission build is a programming failure; provider or
Docker failures are infrastructure outcomes. Invalid artifacts and exhausted model
budgets do not disappear from the denominator. An infrastructure error may still
have diagnostic grading results, but is excluded from completed-run success rates.

## Results and independent review

The results directory is private and outside all Git worktrees:

```text
plan.json                 frozen experimental settings and matrix
environment.json          resolved toolchain image
index.sqlite3             rebuildable aggregate index
runs/<anonymous-id>/
  metadata.json           task/model/settings/starter/prompt provenance
  events.jsonl            redacted request/response/tool/usage trace
  input-manifest.json     exact public export fingerprint
  problem.md              frozen public contract for the reviewer
  baseline/               original source for review context
  source/                 final submitted source
  source.patch            original-to-final diff
  result.json             scores, resource usage and stopping reason
  review.json             immutable human review, once supplied
```

```sh
python3 evals/change-pilots/experiments/review.py \
  --root /path/outside/git/prism-calibration --seed 42
```

Open the printed loopback URL. The review UI hides model identity and correctness
results, shows language/task/source context, times active review (pausing when the
tab is hidden or explicitly paused), and records the decision, located findings,
comprehension answer and confidence. Reveal is a separate action available only
after saving a review. The reviewer still controls external access to the private
results directory: UI blinding cannot erase knowledge from viewing files directly.
Use a preselected balanced subset and counterbalance review order to reduce practice
effects. Finding correctness/false alarms need later adjudication; this UI stores the
evidence but does not pretend test outcomes can automatically judge every finding.

Raw JSON artifacts are authoritative; SQLite can be rebuilt. Metadata, final results
and reviews are written once. Trace copies redact credentials and opaque provider
reasoning; the in-memory continuation retains protocol-required reasoning items.

## Subscriptions

Codex supports ChatGPT subscription authentication, and Claude Code supports Pro/Max
authentication. This machine reported ChatGPT login for Codex and Max/claude.ai login
for Claude Code during setup. That proves login status, not model entitlement or
remaining quota. Subscription execution may have no incremental charge within the
included allocation; it is not unlimited free API access.
[Codex authentication](https://learn.chatgpt.com/docs/auth),
[Claude Code with Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan).

For Claude Code, `ANTHROPIC_API_KEY` overrides subscription authentication, especially
in print mode. The native preparation code removes API credentials from its child
environment. Never repurpose subscription OAuth credentials for the custom API loop.
[Claude authentication precedence](https://code.claude.com/docs/en/env-vars).

The native client container has no host binds, evaluator files or Docker socket.
An exact provider-host CONNECT proxy restricts its network access; the task container
has no network or credentials. Real CLI requests to an offline fake provider expose
the effective tools and exercise the MCP bridge. See [setup and evidence](NATIVE.md).
Installed Claude `--bare` forces API auth and `--safe-mode` also disables custom MCP;
the isolated image uses explicit tool/customization controls instead.
Native experiments must carry a distinct harness label because prompts, compaction,
tool behavior, token accounting and provider routing can differ.

## Verification

```sh
python3 -m unittest discover -s evals/change-pilots/tests -v
PRISM_EVAL_DOCKER_TESTS=1 python3 -m unittest discover \
  -s evals/change-pilots/tests -p test_sandbox.py -v
python3 -m unittest discover -s evals/change-pilots/heldout/tests -v
```

The Docker tests include all nine public starter baselines and filesystem/reset
checks. API continuation, accounting and budget tests use synthetic responses;
they are not scored model runs. These three tasks compare the supplied starter
designs, tooling and documentation as well as languages. More repetitions do not
remove starter-design effects or establish generalization to unrelated tasks.
