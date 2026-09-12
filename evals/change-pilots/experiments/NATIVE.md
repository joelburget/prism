# Isolated subscription clients

The native route uses official Codex 0.154.0 and Claude Code 2.1.257, pinned in a
Docker image. The experiment batch scheduler remains API-only. These tools verify
the native environment and prepare authentication; they do not launch evals.

[Recorded verification](NATIVE_VERIFICATION.json) covers all eight model
configurations, both provider boundaries, and the exact image and source hashes.
All eight passed their offline tool inventory and task-container canary checks.

From `evals/change-pilots`:

```sh
python3 -m experiments.native_setup build
python3 -m experiments.native_check verify --provider openai \
  --model-id gpt-5.6-luna --effort medium --output reports/native-openai.json
python3 -m experiments.native_check verify --provider anthropic \
  --model-id claude-haiku-4-5-20251001 --output reports/native-anthropic.json
python3 -m experiments.native_check status --provider openai
python3 -m experiments.native_check login --provider anthropic
```

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
