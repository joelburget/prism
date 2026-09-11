"""Offline protocol and budget tests; no credentials or paid API requests."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.agent import run_agent
from experiments.providers import ProviderError, normalize_usage, price_usage


def response(output=None, **extra):
    return {"status": "completed", "output": output or [],
            "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20},
                      "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}}, **extra}


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, provider, body, timeout):
        self.requests.append(copy.deepcopy(body))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


class ProvidersTest(unittest.TestCase):
    def config(self, transport, **extra):
        return {"provider": "openai", "model_id": "fixture-model", "reasoning": "medium",
                "max_turns": 3, "max_cost_usd": 10, "max_output_tokens": 1000,
                "pricing": {"input": 2, "cache_read": 0.2, "cache_write": 2.5, "output": 10},
                "_transport": transport, **extra}

    def test_openai_replays_reasoning_and_tool_items(self):
        reasoning = {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "opaque-fixture"}
        call = {"type": "function_call", "name": "execute", "call_id": "call_fixture",
                "arguments": json.dumps({"command": "pwd", "timeout_seconds": 200})}
        transport = ScriptedTransport([response([reasoning, call]), response([
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}])])
        events, commands = [], []
        result = run_agent(self.config(transport), "fix task", lambda cmd, timeout: commands.append((cmd, timeout)) or {"stdout": "/workspace"}, events.append)
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(commands, [("pwd", 120)])
        self.assertEqual(transport.requests[1]["input"][1:3], [reasoning, call])
        self.assertEqual(transport.requests[1]["input"][3]["type"], "function_call_output")
        self.assertFalse(transport.requests[0]["store"])
        self.assertEqual(transport.requests[0]["reasoning"], {"effort": "medium"})
        self.assertEqual(result["usage"]["input_tokens"], 160)
        self.assertEqual(result["usage"]["reasoning_tokens"], 60)
        self.assertAlmostEqual(result["estimated_cost_usd"], 2 * (80 * 2 + 20 * 0.2 + 50 * 10) / 1e6)
        self.assertNotIn("opaque-fixture", json.dumps(events))

    def test_anthropic_preserves_thinking_and_all_tool_results(self):
        thinking = {"type": "thinking", "thinking": "private fixture thought", "signature": "opaque signature"}
        content = [thinking, {"type": "tool_use", "id": "a", "name": "execute", "input": {"command": "ls", "timeout_seconds": 1}},
                   {"type": "tool_use", "id": "b", "name": "execute", "input": {"command": "pwd", "timeout_seconds": 1}}]
        usage = {"input_tokens": 10, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 100,
                 "cache_creation": {"ephemeral_5m_input_tokens": 80, "ephemeral_1h_input_tokens": 20},
                 "output_tokens": 40, "output_tokens_details": {"thinking_tokens": 30}}
        transport = ScriptedTransport([{"content": content, "stop_reason": "tool_use", "usage": usage},
                                       {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": usage}])
        events = []
        result = run_agent(self.config(transport, provider="anthropic", reasoning={"type": "adaptive"}, effort="medium"), "task", lambda *args: {"exit_code": 0}, events.append)
        self.assertEqual(transport.requests[1]["messages"][1]["content"], content)
        self.assertEqual([x["tool_use_id"] for x in transport.requests[1]["messages"][2]["content"]], ["a", "b"])
        self.assertEqual(result["usage"]["input_tokens"], 20)
        self.assertEqual(result["usage"]["cache_write_input_tokens"], 200)
        self.assertAlmostEqual(result["estimated_cost_usd"], 2 * (10*2 + 50*0.2 + 80*2.5 + 20*4 + 40*10) / 1e6)
        self.assertNotIn("private fixture thought", json.dumps(events))

    def test_preflight_stops_before_any_request(self):
        transport = ScriptedTransport([])
        result = run_agent(self.config(transport, max_cost_usd=0.00001), "task", lambda *a: None, lambda event: None)
        self.assertEqual(result["stop_reason"], "cost_budget")
        self.assertEqual(transport.requests, [])
        self.assertEqual(result["estimated_cost_usd"], 0)

    def test_ambiguous_failure_is_not_retried_and_reserves_cost(self):
        transport = ScriptedTransport([ProviderError("timeout; billing unknown"), response()])
        result = run_agent(self.config(transport), "task", lambda *a: None, lambda event: None)
        self.assertEqual(result["stop_reason"], "provider_error")
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(result["billing_unknown"])
        self.assertGreater(result["unreconciled_reservation_usd"], 0)

    def test_incomplete_response_never_executes_partial_calls(self):
        call = {"type": "function_call", "name": "execute", "call_id": "a", "arguments": "{"}
        transport = ScriptedTransport([response([call], status="incomplete")])
        executed = []
        result = run_agent(self.config(transport), "task", lambda *a: executed.append(a), lambda event: None)
        self.assertEqual(result["stop_reason"], "provider_incomplete")
        self.assertFalse(executed)

    def test_fresh_history_each_run_and_hard_turn_cap(self):
        call = {"type": "function_call", "name": "execute", "call_id": "a", "arguments": '{"command":"ls","timeout_seconds":1}'}
        transport = ScriptedTransport([response([call]), response()])
        config = self.config(transport, max_turns=1)
        first = run_agent(config, "one", lambda *a: {}, lambda event: None)
        second = run_agent(config, "two", lambda *a: {}, lambda event: None)
        self.assertEqual(first["stop_reason"], "max_turns")
        self.assertEqual(second["stop_reason"], "completed")
        self.assertEqual(transport.requests[1]["input"], [{"role": "user", "content": "two"}])

    def test_wall_deadline_prevents_next_api_request(self):
        transport = ScriptedTransport([])
        with patch("experiments.agent.time.monotonic", side_effect=[0, 2, 2, 2]):
            result = run_agent(self.config(transport, wall_timeout_seconds=1), "task", lambda *a: None, lambda event: None)
        self.assertEqual(result["stop_reason"], "wall_timeout")
        self.assertEqual(transport.requests, [])

    def test_long_context_pricing_uses_total_including_cached_input(self):
        usage = normalize_usage("openai", response(usage={"input_tokens": 300000, "input_tokens_details": {"cached_tokens": 200000}, "output_tokens": 100}))
        pricing = {"input": 2, "cache_read": 0.2, "cache_write": 2.5, "output": 10,
                   "long_context_threshold": 272000, "input_multiplier": 2, "output_multiplier": 1.5}
        self.assertAlmostEqual(price_usage(usage, pricing), (100000*4 + 200000*0.4 + 100*15)/1e6)

    def test_missing_usage_stops_as_unknown_billing(self):
        transport = ScriptedTransport([{"status": "completed", "output": []}])
        result = run_agent(self.config(transport), "task", lambda *a: None, lambda event: None)
        self.assertTrue(result["billing_unknown"])
        self.assertEqual(result["stop_reason"], "provider_error")


if __name__ == "__main__":
    unittest.main()
