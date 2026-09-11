"""Small, stateless provider adapters. No SDK retries or credential discovery.

Protocol references (checked 2026-09-11):
https://developers.openai.com/api/docs/guides/function-calling
https://developers.openai.com/api/docs/guides/reasoning
https://platform.claude.com/docs/en/api/messages/create
https://platform.claude.com/docs/en/build-with-claude/thinking
https://platform.claude.com/docs/en/build-with-claude/prompt-caching
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import urllib.error
import urllib.request


EXECUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "timeout_seconds": {"type": "number", "minimum": 0.1},
    },
    "required": ["command", "timeout_seconds"],
    "additionalProperties": False,
}
TOOL_DESCRIPTION = "Execute a shell command in the isolated task workspace. Returns stdout, stderr, exit_code, elapsed_seconds, and timed_out."
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
              "cache_write_5m_input_tokens", "cache_write_1h_input_tokens",
              "output_tokens", "reasoning_tokens")


class ProviderError(RuntimeError):
    """A request failed; its billing may be unknown. Never automatically retry."""


def redact(value):
    """Redact auth-shaped values and opaque reasoning material in trace copies."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in {
            "authorization", "x-api-key", "api_key", "api-key", "access_token",
            "encrypted_content", "signature", "thinking", "data",
        } else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", value)
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(provider, body, timeout_seconds, api_key_env=None):
    """Only read the selected environment key at the instant of a live request."""
    env = api_key_env or {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    key = os.environ.get(env)
    if not key:
        raise ProviderError(f"Missing required environment variable {env}; request not sent")
    headers = {"Content-Type": "application/json"}
    if provider == "openai":
        url = "https://api.openai.com/v1/responses"
        headers["Authorization"] = "Bearer " + key
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        # Disable redirects: authorization must never travel to another endpoint.
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout_seconds) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ProviderError("Provider response exceeded 16 MiB; billing unknown")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ProviderError("Provider returned non-object JSON; billing unknown")
        return result
    except urllib.error.HTTPError as exc:
        # Response bodies and exception strings may echo secrets or task content.
        raise ProviderError(f"Provider HTTP {exc.code}; no retry; billing unknown") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise ProviderError("Provider transport or JSON failure; no retry; billing unknown") from None


def _tokens(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("Invalid provider token usage; billing unknown")
    return value


def normalize_usage(provider, response):
    usage = response.get("usage")
    if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
        raise ProviderError("Provider omitted usage; billing unknown")
    result = dict.fromkeys(USAGE_KEYS, 0)
    result["output_tokens"] = _tokens(usage["output_tokens"])
    if provider == "openai":
        cached = _tokens((usage.get("input_tokens_details") or {}).get("cached_tokens", 0))
        total = _tokens(usage["input_tokens"])
        if cached > total:
            raise ProviderError("Invalid cached input usage; billing unknown")
        result["input_tokens"] = total - cached
        result["cache_read_input_tokens"] = cached
        result["reasoning_tokens"] = _tokens((usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0))
    else:
        # Anthropic input_tokens excludes both cache reads AND cache writes.
        result["input_tokens"] = _tokens(usage["input_tokens"])
        result["cache_read_input_tokens"] = _tokens(usage.get("cache_read_input_tokens", 0))
        result["cache_write_input_tokens"] = _tokens(usage.get("cache_creation_input_tokens", 0))
        creation = usage.get("cache_creation") or {}
        result["cache_write_5m_input_tokens"] = _tokens(creation.get("ephemeral_5m_input_tokens", 0))
        result["cache_write_1h_input_tokens"] = _tokens(creation.get("ephemeral_1h_input_tokens", 0))
        result["reasoning_tokens"] = _tokens((usage.get("output_tokens_details") or {}).get("thinking_tokens", 0))
    return result


def price_usage(usage, pricing):
    """Reasoning is a subset of output_tokens, never an additional charge."""
    total_input = sum(usage[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens"))
    long_context = total_input > pricing.get("long_context_threshold", math.inf)
    im = pricing.get("input_multiplier", 2) if long_context else 1
    om = pricing.get("output_multiplier", 1.5) if long_context else 1
    cm = pricing.get("cache_multiplier", im) if long_context else 1
    one_hour = usage.get("cache_write_1h_input_tokens", 0)
    write_other = max(0, usage["cache_write_input_tokens"] - one_hour)
    return (usage["input_tokens"] * pricing["input"] * im
            + usage["cache_read_input_tokens"] * pricing["cache_read"] * cm
            + write_other * pricing["cache_write"] * cm
            + one_hour * pricing.get("cache_write_1h", pricing["input"] * 2) * cm
            + usage["output_tokens"] * pricing["output"] * om) / 1_000_000


class Provider:
    def __init__(self, config):
        self.config = config
        self.name = config["provider"]
        if self.name not in {"openai", "anthropic"}:
            raise ValueError("provider must be openai or anthropic")

    def initial_history(self, prompt):
        return [{"role": "user", "content": prompt}]

    def body(self, history):
        config = self.config
        body = {"model": config["model_id"]}
        if self.name == "openai":
            body.update({"input": copy.deepcopy(history), "store": False,
                         "include": ["reasoning.encrypted_content"],
                         "max_output_tokens": config["max_output_tokens"],
                         "parallel_tool_calls": False,
                         "tools": [{"type": "function", "name": "execute", "description": TOOL_DESCRIPTION,
                                    "parameters": copy.deepcopy(EXECUTE_SCHEMA), "strict": True}]})
            reasoning = config.get("reasoning")
            if reasoning:
                body["reasoning"] = {"effort": reasoning} if isinstance(reasoning, str) else copy.deepcopy(reasoning)
        else:
            body.update({"messages": copy.deepcopy(history), "max_tokens": config["max_output_tokens"],
                         "tools": [{"name": "execute", "description": TOOL_DESCRIPTION,
                                    "input_schema": copy.deepcopy(EXECUTE_SCHEMA)}]})
            if config.get("reasoning"):
                body["thinking"] = copy.deepcopy(config["reasoning"])
            if config.get("effort"):
                body["output_config"] = {"effort": config["effort"]}
        return body

    def request(self, body, timeout_seconds):
        transport = self.config.get("_transport")
        if transport:
            return transport(self.name, body, timeout_seconds)
        return request_json(self.name, body, timeout_seconds, self.config.get("api_key_env"))

    def consume(self, history, response):
        """Return tool calls, final text, stop reason; retain all continuation items."""
        calls, texts = [], []
        if self.name == "openai":
            content = response.get("output", [])
            history.extend(copy.deepcopy(content))
            for item in content:
                if item.get("type") == "function_call":
                    calls.append({"id": item["call_id"], "name": item["name"], "arguments": item["arguments"]})
                elif item.get("type") == "message":
                    texts.extend(block.get("text", block.get("refusal", "")) for block in item.get("content", []))
            status = response.get("status")
            stop = "completed" if status == "completed" else "provider_" + str(status or "missing_status")
        else:
            content = response.get("content", [])
            history.append({"role": "assistant", "content": copy.deepcopy(content)})
            for item in content:
                if item.get("type") == "tool_use":
                    calls.append({"id": item["id"], "name": item["name"], "arguments": item["input"]})
                elif item.get("type") == "text":
                    texts.append(item["text"])
            reason = response.get("stop_reason")
            stop = "completed" if reason in {"end_turn", "tool_use"} else "provider_" + str(reason or "missing_stop_reason")
        return calls, "\n".join(texts), stop

    def add_results(self, history, results):
        if self.name == "openai":
            history.extend({"type": "function_call_output", "call_id": call_id, "output": output} for call_id, output in results)
        else:
            history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": output} for call_id, output in results]})
