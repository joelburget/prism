"""Native readiness and MCP boundary tests; no inference or authentication."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native import native_doctor, prepare_native, run_native, subscription_environment
from experiments.native_bridge import ExecuteBridge


class NativeTest(unittest.TestCase):
    def test_native_execution_is_fail_closed_even_if_config_claims_verified(self):
        events = []
        with patch("subprocess.Popen", side_effect=AssertionError("Must not launch a model")):
            result = run_native({"provider": "openai", "model_id": "fixture-model", "isolation_verified": True},
                                "task", lambda *a: self.fail("Must not execute"), events.append)
        self.assertEqual(result["stop_reason"], "native_isolation_unverified")
        self.assertEqual(result["inference_requests"], 0)
        self.assertIsNone(result["estimated_cost_usd"])
        self.assertEqual(events[0]["event"], "native_blocked")

    def test_preview_keeps_harness_and_subscription_constraints_explicit(self):
        plan = prepare_native({"provider": "anthropic", "model_id": "fixture-model"})
        self.assertEqual(plan["harness"], "subscription-native")
        self.assertFalse(plan["ready"])
        self.assertNotIn("--bare", plan["required_cli_flags"])
        self.assertTrue(any("--bare" in note for note in plan["client_notes"]))
        self.assertNotIn("argv", plan)

    def test_child_environment_excludes_api_keys_and_custom_routing(self):
        supplied = dict.fromkeys(["PATH", "HOME", "LANG", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                  "ANTHROPIC_AUTH_TOKEN", "OPENAI_BASE_URL", "NODE_OPTIONS",
                                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_SIMPLE"], "unused")
        self.assertEqual(set(subscription_environment(supplied)), {"PATH", "HOME", "LANG"})

    def test_doctor_missing_clients_never_probes_auth(self):
        with patch("experiments.native.shutil.which", return_value=None), patch("subprocess.run", side_effect=AssertionError("No subprocess")):
            result = native_doctor()
        self.assertFalse(result["ready"])
        self.assertEqual(result["clients"]["claude"]["auth_status"], "not_checked")

    def bridge(self, **kwargs):
        calls, events = [], []
        bridge = ExecuteBridge(lambda cmd, timeout: calls.append((cmd, timeout)) or {"stdout": "fixture result", "exit_code": 0}, events.append, **kwargs)
        bridge.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        return bridge, calls, events

    def call(self, request_id=1, name="execute", arguments=None):
        return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
            "name": name, "arguments": arguments if arguments is not None else {"command": "pwd", "timeout_seconds": 200}}}

    def test_bridge_exposes_one_tool_and_no_hidden_resources(self):
        bridge, calls, _ = self.bridge()
        listed = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["execute"])
        resources = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        self.assertEqual(resources["result"], {"resources": []})
        self.assertFalse(calls)

    def test_bridge_dispatches_only_to_supplied_callback_and_clamps_timeout(self):
        bridge, calls, events = self.bridge()
        with patch("subprocess.Popen", side_effect=AssertionError("No host shell")):
            result = bridge.handle(self.call())
        self.assertEqual(calls, [("pwd", 120)])
        self.assertFalse(result["result"]["isError"])
        self.assertEqual([event["event"] for event in events], ["tool_call", "tool_result"])

    def test_invalid_tools_duplicate_ids_and_notifications_never_execute(self):
        bridge, calls, _ = self.bridge()
        self.assertIn("error", bridge.handle(self.call(name="host_shell")))
        self.assertIn("error", bridge.handle(self.call()))
        notify = self.call(2)
        notify.pop("id")
        self.assertIsNone(bridge.handle(notify))
        self.assertFalse(calls)

    def test_call_and_wall_budgets_stop_execution(self):
        bridge, calls, _ = self.bridge(max_calls=1)
        bridge.handle(self.call())
        self.assertIn("error", bridge.handle(self.call(2)))
        self.assertEqual(len(calls), 1)
        time_value = [0]
        bridge, calls, _ = self.bridge(wall_timeout_seconds=1, clock=lambda: time_value[0])
        time_value[0] = 2
        self.assertIn("error", bridge.handle(self.call()))
        self.assertFalse(calls)

    def test_output_is_bounded(self):
        bridge, _, _ = self.bridge(max_output_chars=5)
        output = bridge.handle(self.call())["result"]["content"][0]["text"]
        self.assertEqual(output, '{"std\n[tool output truncated]')


if __name__ == "__main__":
    unittest.main()
