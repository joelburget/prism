"""Offline fixture protocol and inventory checks; no native credentials needed."""
from pathlib import Path
import json
from contextlib import redirect_stdout
import io
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native_probe import (CANARY_COMMAND, FixtureServer, inventory_report,
                                      main, messages_events, request_tools, responses_events, tool_names)


class NativeProbeTest(unittest.TestCase):
    def test_entrypoint_preserves_configured_reasoning_effort(self):
        with patch("experiments.native_probe.run_inside", return_value={}) as run, redirect_stdout(io.StringIO()):
            main(["--client", "codex", "--model-id", "gpt-5.6-luna", "--effort", "medium"])
        run.assert_called_once_with("codex", "gpt-5.6-luna", 60, effort="medium")

    def test_inventory_rejects_all_unexpected_tools(self):
        for client in ("codex", "claude"):
            good = inventory_report(client, ["mcp__pilot__execute"])
            self.assertTrue(good["inventory_ok"])
            for tool in ("shell", "exec_command", "apply_patch", "web_search", "spawn_agent", "Read", "Bash", "mcp__other__execute", "mcp__execute__execute", "mcp__execute.execute"):
                report = inventory_report(client, ["mcp__pilot__execute", tool])
                self.assertFalse(report["inventory_ok"])
                self.assertIn(tool, report["unexpected_tools"])
            self.assertFalse(inventory_report(client, [])["inventory_ok"])

    def test_nested_and_builtin_tools_are_not_hidden(self):
        self.assertEqual(tool_names([{"type": "namespace", "name": "functions", "tools": [{"type": "function", "name": "shell"}]},
                                     {"type": "web_search"}, {"type": "function", "function": {"name": "legacy"}}]),
                         ["functions.shell", "web_search", "legacy"])

    def test_responses_lite_additional_tools_and_top_level_are_combined(self):
        body = {"tools": [{"type": "function", "name": "top_level_shell"}], "input": [
            {"type": "message", "role": "user", "content": []},
            {"type": "additional_tools", "role": "developer", "tools": [
                {"type": "namespace", "name": "mcp__pilot", "tools": [{"type": "function", "name": "execute"}]}]}]}
        names = tool_names(request_tools(body))
        self.assertEqual(names, ["top_level_shell", "mcp__pilot.execute"])
        report = inventory_report("codex", names)
        self.assertFalse(report["inventory_ok"])
        self.assertEqual(report["unexpected_tools"], ["top_level_shell"])

    def test_responses_lite_hidden_builtin_is_rejected(self):
        body = {"input": [{"type": "additional_tools", "tools": [
            {"type": "namespace", "name": "mcp__pilot", "tools": [{"type": "function", "name": "execute"}]},
            {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "apply_patch"}]}]}]}
        report = inventory_report("codex", tool_names(request_tools(body)))
        self.assertFalse(report["inventory_ok"])
        self.assertEqual(report["unexpected_tools"], ["functions.apply_patch"])
        body["input"][0]["tools"].append("malformed")
        with self.assertRaises(ValueError):
            tool_names(request_tools(body))

    def test_namespaced_responses_calls_preserve_namespace_separately(self):
        item = responses_events("fixture", "mcp__pilot.execute")[-1]["response"]["output"][0]
        self.assertEqual(item["namespace"], "mcp__pilot")
        self.assertEqual(item["name"], "execute")

    def test_only_codex_has_explicit_benign_resource_tools(self):
        self.assertTrue(inventory_report("codex", ["mcp__pilot__execute", "list_mcp_resources"])["inventory_ok"])
        self.assertFalse(inventory_report("claude", ["mcp__pilot__execute", "list_mcp_resources"])["inventory_ok"])

    def test_responses_fixture_returns_fixed_execute_arguments(self):
        events = responses_events("fixture", "mcp__pilot__execute")
        final = events[-1]["response"]
        self.assertEqual(final["status"], "completed")
        call = final["output"][0]
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(json.loads(call["arguments"]), {"command": CANARY_COMMAND, "timeout_seconds": 10})
        self.assertEqual(responses_events("fixture")[-1]["response"]["output"][0]["type"], "message")

    def test_messages_fixture_returns_fixed_execute_arguments(self):
        events = messages_events("fixture", "mcp__pilot__execute")
        self.assertEqual(events[1]["content_block"]["type"], "tool_use")
        self.assertEqual(json.loads(events[2]["delta"]["partial_json"])["command"], CANARY_COMMAND)
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(messages_events("fixture")[-2]["delta"]["stop_reason"], "end_turn")

    def test_http_fixture_never_calls_when_builtin_tool_leaks(self):
        fixture = FixtureServer("codex")
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{fixture.server_port}/v1/responses"
            body = json.dumps({"model": "fixture", "tools": [
                {"type": "function", "name": "mcp__pilot__execute"},
                {"type": "function", "name": "apply_patch"}]}).encode()
            with urlopen(Request(url, body, {"Content-Type": "application/json"}), timeout=3) as response:
                output = response.read().decode()
            self.assertNotIn('"type": "function_call"', output)
            self.assertFalse(fixture.tool_call_issued)
            self.assertEqual(fixture.requests[0]["unexpected_tools"], ["apply_patch"])
        finally:
            fixture.shutdown()
            fixture.server_close()

    def test_http_fixture_captures_inventory_and_calls_execute_once(self):
        fixture = FixtureServer("codex")
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{fixture.server_port}/v1/responses"
            body = json.dumps({"model": "fixture", "tools": [{"type": "function", "name": "mcp__pilot__execute"}]}).encode()
            outputs = []
            for _ in range(2):
                with urlopen(Request(url, body, {"Content-Type": "application/json"}), timeout=3) as response:
                    outputs.append(response.read().decode())
            self.assertIn('"type": "function_call"', outputs[0])
            self.assertNotIn('"type": "function_call"', outputs[1])
            self.assertEqual(len(fixture.requests), 2)
            self.assertTrue(fixture.requests[0]["inventory_ok"])
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    unittest.main()
