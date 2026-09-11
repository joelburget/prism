"""One shared coding-agent loop for both APIs; the caller supplies the sandbox.

run_agent(config, prompt, execute, record) starts a fresh conversation. execute
accepts (command, timeout_seconds) and returns a JSON-compatible result. record
receives a single redacted event dictionary. _transport is a test-only injection
accepting (provider_name, request_body, timeout_seconds), returning provider JSON.

Costs are estimates, not an account-level billing guarantee: preflight reserves
one token per UTF-8 request byte plus protocol overhead and the full output cap.
Provider-side tokenization, changing tariffs, and ambiguous failures cannot be
reconciled without provider billing data. Unknown billing stops the run immediately.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import signal
import threading
import time

from .providers import Provider, ProviderError, USAGE_KEYS, normalize_usage, price_usage, redact


@contextmanager
def _deadline(seconds):
    """Bound blocking HTTP including slow reads, on the supported POSIX main thread."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise RuntimeError("Agent requests require a POSIX main thread for the hard wall deadline")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0] or old_timer[1]:
        raise RuntimeError("Cannot replace an existing SIGALRM deadline")

    def expired(signum, frame):
        raise TimeoutError("Agent wall deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _positive(config, key, default, integer=False):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be positive and finite")
    if integer and not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    config[key] = value
    return value


def preflight_cost(body, config):
    """Conservative local estimate with zero cache discount and full output cap."""
    # JSON bytes also overcount escaped content, structural fields, and opaque
    # reasoning. Overhead covers provider-rendered tool instructions and framing.
    estimated_input = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 4096
    pricing = config["pricing"]
    input_rate = max(pricing["input"], pricing["cache_read"], pricing["cache_write"],
                     pricing.get("cache_write_1h", 0))
    im = pricing.get("input_multiplier", 2) if estimated_input > pricing.get("long_context_threshold", math.inf) else 1
    om = pricing.get("output_multiplier", 1.5) if estimated_input > pricing.get("long_context_threshold", math.inf) else 1
    return estimated_input, (estimated_input * input_rate * im + config["max_output_tokens"] * pricing["output"] * om) / 1_000_000


def run_agent(config, prompt, execute, record):
    config = dict(config)
    max_turns = _positive(config, "max_turns", 30, integer=True)
    _positive(config, "max_output_tokens", 8192, integer=True)
    wall_limit = _positive(config, "wall_timeout_seconds", 1200)
    call_limit = _positive(config, "request_timeout_seconds", 300)
    tool_limit = _positive(config, "tool_timeout_seconds", 120)
    spend_limit = _positive(config, "max_cost_usd", 2)
    output_limit = _positive(config, "max_tool_output_chars", 32000, integer=True)
    if "max_input_tokens" in config:
        _positive(config, "max_input_tokens", 1, integer=True)
    for field in ("input", "cache_read", "cache_write", "output"):
        rate = config.get("pricing", {}).get(field)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
            raise ValueError(f"pricing.{field} must be a nonnegative finite USD/Mtoken rate")
    for field in ("long_context_threshold", "input_multiplier", "output_multiplier", "cache_multiplier", "cache_write_1h"):
        if field in config["pricing"]:
            rate = config["pricing"][field]
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
                raise ValueError(f"pricing.{field} must be positive and finite")
    if not isinstance(config.get("model_id"), str) or not config["model_id"]:
        raise ValueError("model_id is required")
    provider = Provider(config)
    history = provider.initial_history(prompt)
    usage = dict.fromkeys(USAGE_KEYS, 0)
    started = time.monotonic()
    deadline = started + wall_limit
    turns, tool_calls, observed_cost, unknown_reservation = 0, 0, 0.0, 0.0
    stop, final_text, error = "max_turns", "", None

    def emit(kind, **fields):
        record(redact({"event": kind, "turn": turns, "elapsed_seconds": time.monotonic() - started, **fields}))

    for _ in range(max_turns):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop = "wall_timeout"
            break
        body = provider.body(history)
        estimated_input, reserve = preflight_cost(body, config)
        if config.get("max_input_tokens") and estimated_input > config["max_input_tokens"]:
            stop = "input_budget"
            break
        if observed_cost + reserve > spend_limit:
            stop = "cost_budget"
            emit("budget_stop", estimated_input_tokens=estimated_input, next_request_reservation_usd=reserve)
            break
        turns += 1
        emit("request", provider=provider.name, body=body, estimated_input_tokens=estimated_input,
             reservation_usd=reserve)
        try:
            with _deadline(min(remaining, call_limit)):
                response = provider.request(body, min(remaining, call_limit))
            emit("response", provider=provider.name, body=response)
            turn_usage = normalize_usage(provider.name, response)
            turn_cost = price_usage(turn_usage, config["pricing"])
        except (ProviderError, TimeoutError, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
            stop = "provider_error"
            unknown_reservation = reserve
            error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            emit("provider_error", error=error, billing_unknown=True, reservation_usd=reserve)
            break
        for key in usage:
            usage[key] += turn_usage[key]
        observed_cost += turn_cost
        emit("usage", usage=turn_usage, estimated_cost_usd=turn_cost)
        try:
            calls, text, provider_stop = provider.consume(history, response)
        except (KeyError, TypeError, ValueError, AttributeError):
            stop, error = "provider_protocol_error", "Malformed provider response"
            break
        if text:
            final_text = text
        if observed_cost >= spend_limit:
            stop = "cost_budget"
            break
        if time.monotonic() >= deadline:
            stop = "wall_timeout"
            break
        if provider_stop != "completed":
            stop = provider_stop
            break
        if not calls:
            stop = "completed"
            break
        results = []
        for call in calls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop = "wall_timeout"
                break
            try:
                args = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
                if call["name"] != "execute" or not isinstance(args, dict) or set(args) != {"command", "timeout_seconds"}:
                    raise ValueError("invalid tool arguments")
                timeout = args["timeout_seconds"]
                if not isinstance(args["command"], str) or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError("invalid tool arguments")
            except (ValueError, TypeError, KeyError):
                stop, error = "invalid_tool_call", "Expected execute(command: string, timeout_seconds: positive number)"
                break
            timeout = min(timeout, tool_limit, remaining)
            emit("tool_call", call_id=call["id"], command=args["command"], timeout_seconds=timeout)
            try:
                # The supplied sandbox must enforce this timeout and kill descendants.
                result = execute(args["command"], timeout)
                output = json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                stop, error = "tool_error", type(exc).__name__
                emit("tool_error", error=error)
                break
            tool_calls += 1
            if len(output) > output_limit:
                output = output[:output_limit] + "\n[tool output truncated]"
            emit("tool_result", call_id=call["id"], output=output)
            results.append((call["id"], output))
            # The Docker boundary kills the container on timeout/output overflow.
            # These are model resource-limit outcomes, not a subsequent infra error.
            if isinstance(result, dict) and (result.get("timed_out") or result.get("output_limited")):
                stop = "tool_timeout" if result.get("timed_out") else "tool_output_limit"
                break
        if stop in {"wall_timeout", "invalid_tool_call", "tool_error", "tool_timeout", "tool_output_limit"}:
            break
        provider.add_results(history, results)
        if time.monotonic() >= deadline:
            stop = "wall_timeout"
            break
    result = {"stop_reason": stop, "usage": usage, "estimated_cost_usd": observed_cost + unknown_reservation,
              "observed_cost_usd": observed_cost, "unreconciled_reservation_usd": unknown_reservation,
              "billing_unknown": unknown_reservation > 0, "elapsed_seconds": time.monotonic() - started,
              "turns": turns, "tool_calls": tool_calls, "final_text": redact(final_text)}
    if error:
        result["error"] = error
    emit("stop", result=result)
    return result
