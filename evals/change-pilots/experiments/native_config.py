"""Pinned native-client launch policy for the empty, isolated client image.

Flags reduce model tool exposure; the container is the filesystem boundary.
Runtime helpers only construct arguments; the image-build helper pins public
model metadata. This module never inspects auth or launches a CLI.
Offline native inventory probes must confirm the effective tools before use.
"""
from __future__ import annotations

import hashlib
import json
import re

RELAY_COMMAND = "python3"
RELAY_ARGS = ["/opt/pilot/native_relay.py", "client"]
MCP_SERVER_NAME = "pilot"
CODEX_CATALOG_PATH = "/opt/pilot/codex-models.json"
CODEX_CATALOG_URL = "https://raw.githubusercontent.com/openai/codex/rust-v0.154.0/codex-rs/models-manager/models.json"
CODEX_CATALOG_SHA256 = "f3b8104396daf6381bed9d7c4b154a8664f9b5089b44b01a9d54f421566ec9a7"
CODEX_TOOL_METADATA = {
    "tool_mode": "direct",
    "apply_patch_tool_type": None,
    "supports_search_tool": False,
    "multi_agent_version": None,
    "node_repl_disabled": True,
    "experimental_supported_tools": [],
}


def restricted_codex_catalog(raw):
    """Preserve the pinned official catalog except documented tool routing.

    Model-owned tool settings take precedence over feature flags in 0.154.0.
    This is an explicit native harness configuration: direct MCP replaces
    Code Mode, deferred discovery, apply_patch, and model-selected subagents.
    Official model IDs, prompts, reasoning options, and context data remain.
    """
    if hashlib.sha256(raw).hexdigest() != CODEX_CATALOG_SHA256:
        raise ValueError("Pinned Codex model catalog digest mismatch")
    catalog = json.loads(raw)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list) or not catalog["models"]:
        raise ValueError("Invalid pinned Codex model catalog")
    for model in catalog["models"]:
        model.update(CODEX_TOOL_METADATA)
    return catalog


def build_codex_catalog():
    """Image-build-only public download; no auth, startup, or runtime fetch."""
    from pathlib import Path
    from urllib.request import urlopen
    with urlopen(CODEX_CATALOG_URL, timeout=30) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    catalog = restricted_codex_catalog(raw)
    Path(CODEX_CATALOG_PATH).write_text(json.dumps(catalog, ensure_ascii=False) + "\n", encoding="utf-8")


# Observed in codex 0.154.0 `features list`. Disable both established and
# alternate routes; ignored/removed feature flags are not an isolation claim.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot",
    "apply_patch_freeform", "js_repl", "js_repl_tools_only",
    "apps", "enable_mcp_apps", "plugins", "remote_plugin", "recommended_plugins",
    "plugin_sharing", "hooks", "skill_search", "skill_mcp_dependency_install",
    "memories", "external_agent_memory_import", "chronicle",
    "multi_agent", "multi_agent_v2", "collaboration_modes", "goals",
    "code_mode", "code_mode_host", "code_mode_only", "code_mode_prewarm",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "in_app_browser", "in_app_local_automation",
    "image_generation", "view_image", "sleep_tool", "tool_suggest",
    "standalone_web_search",
    "request_permissions_tool", "default_mode_request_user_input",
    "workspace_dependencies", "auth_elicitation", "tool_call_mcp_elicitation",
)


def codex_config_overrides():
    values = [f"features.{feature}=false" for feature in CODEX_DISABLED_FEATURES]
    values.extend([
        "features.skip_host_skill_discovery=true",
        'approval_policy="never"', 'sandbox_mode="read-only"',
        'web_search="disabled"', "agents.enabled=false",
        "tools.update_plan.enabled=false", "tools.experimental_request_user_input.enabled=false",
        "project_doc_max_bytes=0", "model_catalog_json=" + json.dumps(CODEX_CATALOG_PATH),
        "skills.bundled.enabled=false", "skills.include_instructions=false",
        "analytics.enabled=false", "check_for_update_on_startup=false",
        "mcp_servers={pilot={command=" + json.dumps(RELAY_COMMAND)
        + ",args=" + json.dumps(RELAY_ARGS)
        + ',required=true,enabled_tools=["execute"],startup_timeout_sec=15,'
          'tool_timeout_sec=180,default_tools_approval_mode="auto",'
          'tools={execute={approval_mode="approve"}},'
          'supports_parallel_tool_calls=false}}',
    ])
    return values


def claude_mcp_config():
    return {"mcpServers": {MCP_SERVER_NAME: {
        "type": "stdio", "command": RELAY_COMMAND, "args": list(RELAY_ARGS),
    }}}


def client_argv(client, model_id, effort=None, *, max_turns=30):
    """Build the fixed launch command. Supply the task prompt through stdin."""
    if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model_id):
        raise ValueError("An explicit model id is required")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")
    client = {"openai": "codex", "anthropic": "claude"}.get(client, client)
    if effort is not None and effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported native reasoning effort")
    if client == "codex":
        argv = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--strict-config", "--skip-git-repo-check", "--json", "--color", "never",
                "--model", model_id]
        for value in codex_config_overrides():
            argv.extend(["-c", value])
        if effort is not None:
            argv.extend(["-c", "model_reasoning_effort=" + json.dumps(effort)])
        return argv
    if client == "claude":
        argv = ["claude", "--print", "--verbose", "--output-format", "stream-json",
                "--model", model_id, "--max-turns", str(max_turns),
                "--tools", "", "--allowedTools", "mcp__pilot__execute",
                "--permission-mode", "dontAsk", "--strict-mcp-config",
                "--mcp-config", json.dumps(claude_mcp_config(), separators=(",", ":")),
                "--setting-sources", "", "--disable-slash-commands", "--no-chrome",
                "--no-session-persistence", "--settings", json.dumps({
                    "disableAllHooks": True, "autoMemoryEnabled": False,
                    "enableAllProjectMcpServers": False,
                }, separators=(",", ":"))]
        if effort is not None:
            argv.extend(["--effort", effort])
        return argv
    raise ValueError("client must be codex or claude")
