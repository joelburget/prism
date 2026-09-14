# Isolated subscription clients

The native route uses official Codex 0.154.0 and Claude Code 2.1.257, pinned in a
Docker image. `native_check` verifies the environment and prepares authentication;
`native_batch` runs independent programming evaluations through the subscriptions.

[Recorded verification](NATIVE_VERIFICATION.json) covers all eight model
configurations, both provider boundaries, and the exact image and source hashes.
All eight passed their offline tool inventory and task-container canary checks.

From `evals/change-pilots`:

```sh
python3 experiments/build_image.py
python3 -m experiments.native_setup build
python3 -m experiments.native_task_setup
python3 -m experiments.native_check verify-all --output experiments/NATIVE_VERIFICATION.json
python3 -m experiments.native_check verify --provider openai \
  --model-id gpt-5.6-luna --effort medium --output reports/native-openai.json
python3 -m experiments.native_check verify --provider anthropic \
  --model-id claude-haiku-4-5-20251001 --output reports/native-anthropic.json
python3 -m experiments.native_check status --provider openai
python3 -m experiments.native_check login --provider anthropic

# Eight models × three languages × ledger refunds × one repetition.
python3 -m experiments.native_batch plan --results /path/outside/git/native-calibration
python3 -m experiments.native_batch run --results /path/outside/git/native-calibration
python3 -m experiments.native_batch status --results /path/outside/git/native-calibration
```

The native batch plan freezes both image IDs, verified model/effort combinations,
source and starter hashes, prompts, and randomized order. Runs are serial, use a
fresh client and task container for each cell, and save the same source, grading,
trace and blinded-review artifacts as the API runner. Default limits are 100 tool
calls and 30 minutes per rollout, with an additional 100-turn limit for Claude.
Native CLI defaults control token limits; no shared token or dollar cap is claimed.
No API keys or automatic paid fallback are used. Infrastructure failures stop the
batch and stay separate from ability scores; finished cells are never retried.
The legacy `native.py` preview remains disabled; use the `native_batch` entrypoint.
To pause after the active rollout, create `STOP_AFTER_CURRENT` in the results
directory; remove it before resuming. Native completion records are saved before
grading so interrupted grading preserves the agent's output and usage.

Protocol `subscription-native-v2` fixes a mismatch discovered during the first
calibration attempt: Codex's client permissions said `read-only`, and its native
instructions required an `apply_patch` command absent from the task image. A model
therefore stopped without edits. The four attempted v1 cells remain retained in
their original results directory and are excluded from the corrected comparison.
Problem descriptions, starters, acceptance cases and rollout limits did not change.
V2 uses `workspace-write` in Codex's configuration while retaining the whole-client
Docker boundary, and supplies the genuine pinned Codex patch helper plus npm's
existing `npx` entrypoint in every language's task image. The native task prompt
explicitly describes the writable remote workspace and available editor.

The task image includes the public Codex binary because its `apply_patch` invocation
selects the patch parser. It has no authentication/configuration files, network or
host mounts. Both inputs are pinned by immutable image ID; its build context
contains only `NativeTaskDockerfile`. Current builds use Prism 0.22.0; Python, Node,
TypeScript, Codex, and Claude Code remain at their existing pinned versions.
See [0.22 migration validation](VALIDATION-0.22.md) for image identities, baseline
checks, tutorial verification, and fresh performance calibration.

`verify-all` refreshes the planner receipt for all eight configured model/effort
pairs using offline fixtures and verifies both provider boundaries. It makes no
real model inference requests. The previous receipt cannot authorize a new image.

Verification uses a disposable blank authentication volume and a fake provider
inside a network-disabled container. It captures the actual CLI tool inventory,
supplies a canned tool call, and checks that the separate task container executed
the canary. It then tests the same immutable image's container/network boundary.
No real inference or account credentials participate. Repeat for every model and
effort in a proposed experiment: native tool inventories can depend on the model.

Login runs the official client's interactive browser flow. Provider-specific Docker
volumes (`prism-native-auth-openai`, `prism-native-auth-anthropic`) retain its cache;
the client, proxy and networks are removed when the command ends. No host API keys
are forwarded. For OpenAI, `import-codex-login --provider openai` copies the existing
`~/.codex/auth.json` directly into the Codex volume, without decoding or logging it.
This follows the [official headless-container authentication instructions](https://learn.chatgpt.com/docs/auth).
Claude uses a fresh [subscription login](https://code.claude.com/docs/en/authentication),
with `--claudeai`; the Mac keychain is not exported. Authentication status reports
contain only the method, readiness and subscription type, without account details.

The native CLI lives in a nonroot container with a read-only root, no capabilities,
no host mounts, no evaluator data, no Docker socket, and only its own auth volume.
Its internal network has no external gateway or DNS forwarding. A separate,
credential-free proxy permits TLS CONNECT only to exact provider/auth domains;
HTTP, IP literals, other ports, private destinations and other domains are denied.
The proxy forwards encrypted traffic without inspecting or recording credentials.
Docker and the installed native binaries remain trusted infrastructure.

The only task tool is `execute`. A fixed Unix socket connects the CLI's MCP client
to a host controller through Docker-exec stdio. The controller chooses the callback
in advance: commands run in the existing network-disabled `DockerSandbox`, which
has task files but no credentials. The client cannot select another container or
host endpoint. RPC framing, request IDs, tool arguments, call count, time and output
are bounded. Native session traces and usage have a separate `subscription-native`
label; CLI dollar estimates are API-equivalent estimates, not subscription charges.

Codex requires more than feature flags. Its model catalog can independently enable
Code Mode, patching and deferred tool discovery. The image therefore downloads the
official `rust-v0.154.0` catalog with a pinned SHA256 and changes only these tool
fields: `tool_mode=direct`, `apply_patch_tool_type=null`, `supports_search_tool=false`,
`multi_agent_version=null`, `node_repl_disabled=true`, `experimental_supported_tools=[]`.
The last setting removes Astra's additional clock and asynchronous user-input tools.
Model IDs, instructions,
reasoning options and context metadata stay intact. The supported
`model_catalog_json` setting selects that immutable file. This deliberately changes
native tool routing and must be recorded as part of the harness. Codex's benign
empty MCP resource discovery tools are allowed; all other unexpected tools fail
verification. Only `pilot.execute` receives explicit tool approval; the global
approval policy remains `never`. The verifier reads both ordinary tool schemas
and the `additional_tools` input messages used by Codex's Responses Lite protocol.
Claude receives an empty builtin tool list and only the pilot MCP
server, with hooks, memory, skills, browser and session persistence disabled.

Offline success does not establish subscription entitlement, actual model identity
or fallback behavior, remaining quota, or live accounting. Those need a small live
calibration before scheduling the full matrix. Model errors must remain visible;
silently replacing an unavailable model would invalidate the comparison.

## Sequential change calibration

`python3 -m experiments.chained_batch` reuses the same verified per-cell native
execution for two query/workflow checkpoints. Its default plan contains 48 chains
(96 stages), retaining the ledger calibration's per-stage budgets. See the
[follow-up guide](../followups/README.md) for frozen planning, predecessor export,
separate performance screening, and review instructions. Model/language results
are grouped by checkpoint in the result index; the chain controller additionally
reports conditional and unconditional success. It does not repair predecessors,
transfer private grades, resume native conversations, or switch to API billing.

### Recovering the NUL-command classification failure

Literal NUL bytes are legal in JSON strings but cannot be passed in process
arguments. The execute bridge now returns a recoverable tool error before invoking
the sandbox. The rejected attempt still consumes one tool call. Unexpected callback
errors remain infrastructure failures; timeout and source-capture policies are
unchanged.

A completed native session that was incorrectly stopped by this specific error can
be reconciled without another model call:

```sh
PYTHONPATH=evals/change-pilots python3 -m experiments.reconcile \
  --previous /path/to/stopped-results --output /path/to/new-continuation \
  --run INTERRUPTED_RUN_ID
PYTHONPATH=evals/change-pilots python3 -m experiments.chained_batch run \
  --results /path/to/new-continuation
```

Reconciliation requires trace evidence of the NUL failure followed by recovered
tool execution, normal client completion, unchanged frozen source, and unchanged
task/scoring inputs. It copies earlier records verbatim, grades only the interrupted
submission's frozen source, and retains its original result alongside a correction
receipt. The new plan preserves run assignments, prompts, ordering, runtime images,
budgets and performance calibration. It identifies inherited records and the
original plan; subsequent stages use the corrected bridge. The original results
root is preserved. This command does not retry unfinished model sessions or repair
submitted programs.

### Recovering a completed Codex reconnect

Codex can emit a `Reconnecting... N/M (stream disconnected before completion: ...)`
error event, recover within the same session, and finish with `turn.completed` and
exit code zero. The harness accepts that narrow event sequence. Unknown errors,
`turn.failed`, reconnects after completion, a new unfinished turn, nonzero exits,
and relay failures remain infrastructure failures. No retries or budget changes
are introduced by this classification fix.

A previously misclassified frozen submission can be graded without inference:

```sh
PYTHONPATH=evals/change-pilots python3 -m experiments.reconcile \
  --previous /path/to/stopped-results \
  --output /path/to/new-continuation \
  --run RUN_ID --reason codex-reconnect
```

This requires the recorded reconnect and subsequent terminal success, no tool or
resource failure, and unchanged source. It preserves the original result and
measurements, checks inherited records from previous continuations, and copies
all other completed records unchanged. Only the client-classification and
reconciliation code may differ; assignments, task inputs, budgets, runtime images,
and calibration must match. Resume the new plan with the chained batch runner.

### Deadline shutdown and missing submissions

A killed native client can leave a buffered relay reply whose final flush raises
`BrokenPipeError`. Cleanup now tolerates that specific closed-peer error so it
cannot mask the native timeout result and prevent source capture. If the wall
budget and a relay disconnect are observed together, the wall timeout takes
precedence; a relay failure observed before the deadline still stops the run as
infrastructure. Other cleanup errors remain visible.

Regression tests use an actual buffered OS pipe with its reader closed, plus
controller coverage that a wall-time-limited submission is captured and graded.
An offline Docker check also kills a client at its deadline and confirms the task
source can still be frozen and saved. No model inference is required for these checks.

The original final Opus/query checkpoint-two attempt in the 0.22 tutorial cohort
lost its source capture to this bug. Its task container had already been removed,
so ordinary no-inference reconciliation cannot recover a submission. The explicitly
authorized recovery preserves the 31 other stage directories unchanged and starts
one fresh replacement from the frozen first-checkpoint source, with the same model,
prompt, image, effort and budgets. The failed attempt and its trace remain in the
original results root and in the recovery archive. A retry is recorded as such;
it is not represented as recovery of the original model output.
