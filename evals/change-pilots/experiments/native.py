"""Subscription-native plan preview. Batch execution remains fail-closed.

This is a different harness from the shared API loop: native clients supply
their own system prompts, compaction, routing, and accounting. Use native_check
for real container/inventory verification and native_session for bounded probes.
The experiment scheduler has not yet integrated that route. This module never
launches inference, reads OAuth files, or exports a token to an API client.

Evidence checked 2026-09-11: installed codex 0.154.0 / claude 2.1.257 help;
https://developers.openai.com/codex/config-schema.json
https://code.claude.com/docs/en/cli-usage
https://code.claude.com/docs/en/security
"""
from __future__ import annotations

import os
import shutil
import subprocess


SOURCES = ["https://developers.openai.com/codex/config-schema.json",
           "https://code.claude.com/docs/en/cli-usage",
           "https://code.claude.com/docs/en/security"]
REQUIRED_FLAGS = {
    "codex": ["--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json", "--strict-config"],
    "claude": ["--tools", "--allowedTools", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"],
}
BLOCKERS = [
    "The batch scheduler does not yet consume native container/inventory verification or native session results; use experiments.native_check for current per-model evidence.",
    "An isolated subscription login is required separately for each provider; host login status alone is insufficient.",
    "Subscription model availability, automatic fallback behavior, per-request output limits, and accounting need verification in the isolated client.",
]


def subscription_environment(environ=None):
    """Allow only ordinary CLI startup variables; no API credentials or routing."""
    environ = os.environ if environ is None else environ
    keep = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "CODEX_HOME")
    return {key: environ[key] for key in keep if key in environ}


def native_doctor():
    """Read versions/help only. Auth is deliberately not probed or inspected."""
    clients = {}
    for name, flags in REQUIRED_FLAGS.items():
        executable = shutil.which(name)
        report = {"installed": executable is not None, "auth_status": "not_checked", "ready": False}
        if executable:
            try:
                version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                         timeout=10, check=False, env=subscription_environment())
                argv = [executable, "exec", "--help"] if name == "codex" else [executable, "--help"]
                help_result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                                             check=False, env=subscription_environment())
                report.update(version=version.stdout.strip()[:200],
                              missing_flags=[flag for flag in flags if flag not in help_result.stdout],
                              probe_ok=version.returncode == 0 and help_result.returncode == 0)
            except (OSError, subprocess.TimeoutExpired):
                report["probe_ok"] = False
        clients[name] = report
    return {"harness": "subscription-native", "ready": False, "clients": clients,
            "blockers": list(BLOCKERS), "sources": list(SOURCES), "inference_requests": 0}


def prepare_native(config):
    """Return reviewable requirements, deliberately not an executable launch command."""
    provider = config.get("provider")
    client = {"openai": "codex", "anthropic": "claude", "codex": "codex", "claude": "claude"}.get(provider)
    if not client:
        raise ValueError("native provider must be openai/anthropic or codex/claude")
    requirements = [
        "Run a pinned native CLI in a separately authenticated OS/container boundary with no host worktree, evaluator files, or Docker socket mounts.",
        "Use native subscription login inside that boundary; do not extract OAuth credentials or set API-key environment variables.",
        "Expose only a fixed execute MCP bridge whose callback is the already-created DockerSandbox.execute; shell commands stay in its network-disabled task container.",
        "Verify the effective tool list contains only execute, and deny built-in shell/file/browser/search/agent tools, other MCP servers, hooks, skills, memory, and project instructions.",
        "Enforce wall/tool/turn/output limits and record native model identity and subscription usage separately from shared-API results; reject fallback models.",
    ]
    notes = (["Codex read-only sandbox governs generated shell commands; it does not establish a filesystem boundary around the entire native CLI.",
              "--ignore-user-config preserves CODEX_HOME authentication; separate controls are still needed for project/managed configuration and tool discovery."]
             if client == "codex" else [
                 "Installed Claude --bare requires API-key/apiKeyHelper authentication and bypasses subscription OAuth; do not use it for this route.",
                 "--safe-mode disables MCP customizations too; supplied execute MCP availability must be verified, not assumed.",
                 "Admin-managed policy can still apply under --safe-mode, including policy-configured hooks; --tools empty alone is insufficient isolation."])
    return {"harness": "subscription-native", "status": "blocked_batch_not_integrated", "ready": False,
            "client": client, "model_id": config.get("model_id"), "required_cli_flags": REQUIRED_FLAGS[client],
            "requirements": requirements, "client_notes": notes, "blockers": list(BLOCKERS), "sources": list(SOURCES)}


def run_native(config, prompt, execute, record):
    """Never turn an unverified preview into an authenticated model invocation."""
    plan = prepare_native(config)
    result = {"harness": "subscription-native", "stop_reason": "native_batch_not_integrated",
              "ready": False, "model_id": config.get("model_id"), "turns": 0, "tool_calls": 0,
              "inference_requests": 0, "elapsed_seconds": 0, "usage": {},
              "estimated_cost_usd": None, "subscription_usage": "not_run",
              "billing_unknown": False, "final_text": "", "blockers": plan["blockers"]}
    record({"event": "native_blocked", "result": result})
    return result
