"""Real subprocess/socket MCP tests. No provider, CLI, auth, or Docker needed."""
import io
import hashlib
import json
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native_bridge import ExecuteBridge
from experiments.native_config import client_argv, claude_mcp_config, restricted_codex_catalog, CODEX_TOOL_METADATA
from experiments.native_relay import MAX_FRAME_BYTES, ProtocolError, read_frame, write_frame


class NativeRelayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pilot-relay-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.socket_path = self.temp.name + "/relay.sock"
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()

    def launch(self, role):
        # Only test code overrides the module constant. The production CLI has
        # no socket path or environment-based destination configuration.
        code = ("from experiments import native_relay as r; "
                f"r.SOCKET_PATH={self.socket_path!r}; raise SystemExit(r.main([{role!r}]))")
        process = subprocess.Popen([sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def server(self):
        process = self.launch("server")
        with selectors.DefaultSelector() as ready:
            ready.register(process.stderr, selectors.EVENT_READ)
            self.assertTrue(ready.select(5), "Relay did not signal readiness")
        self.assertEqual(process.stderr.readline(), b"pilot-relay-ready\n")
        self.assertEqual(stat.S_IMODE(Path(self.socket_path).stat().st_mode), 0o600)
        return process

    def test_real_stdio_roundtrip_initialize_discover_execute_notifications_and_duplicate_ids(self):
        server = self.server()
        calls, received, errors = [], [], []
        bridge = ExecuteBridge(lambda cmd, timeout: calls.append((cmd, timeout)) or
                               {"stdout": "sandbox result", "exit_code": 0}, lambda _: None)

        def host():
            try:
                while (request := read_frame(server.stdout)) is not None:
                    received.append(request)
                    response = bridge.handle(request)
                    if response is not None:
                        write_frame(server.stdin, response)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=host, daemon=True)
        worker.start()
        client = self.launch("client")
        requests = [
            {"jsonrpc": "2.0", "id": "init", "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "execute", "arguments": {"command": "pwd", "timeout_seconds": 1}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "execute", "arguments": {"command": "pwd", "timeout_seconds": 1}}},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {
                "name": "execute", "arguments": {"command": "never", "timeout_seconds": 1}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        raw = b"".join(json.dumps(request).encode() + b"\n" for request in requests)
        out, err = client.communicate(raw, timeout=10)
        self.assertEqual((client.returncode, err), (0, b""))
        self.assertEqual(server.wait(timeout=5), 0)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(received, requests)
        responses = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([response["id"] for response in responses], ["init", 2, 3, 3, 4])
        self.assertEqual([tool["name"] for tool in responses[1]["result"]["tools"]], ["execute"])
        self.assertIn("sandbox result", responses[2]["result"]["content"][0]["text"])
        self.assertIn("error", responses[3])
        self.assertEqual(calls, [("pwd", 1)])
        self.assertFalse(Path(self.socket_path).exists())

    def test_bad_host_response_fails_without_forwarding_wrong_id(self):
        server = self.server()
        client = self.launch("client")
        write_frame(client.stdin, {"jsonrpc": "2.0", "id": "one", "method": "ping"})
        self.assertEqual(read_frame(server.stdout)["id"], "one")
        write_frame(server.stdin, {"jsonrpc": "2.0", "id": "another", "result": {}})
        out, err = client.communicate(timeout=5)
        self.assertEqual(out, b"")
        self.assertEqual(client.returncode, 1)
        self.assertIn(b"ProtocolError", err)
        self.assertEqual(server.wait(timeout=5), 1)

    def test_oversized_client_input_never_reaches_host(self):
        server = self.server()
        client = self.launch("client")
        out, err = client.communicate(b"x" * (MAX_FRAME_BYTES + 1) + b"\n", timeout=5)
        self.assertEqual(out, b"")
        self.assertEqual(client.returncode, 1)
        self.assertIn(b"ProtocolError", err)
        self.assertEqual(server.wait(timeout=5), 0)
        self.assertEqual(server.stdout.read(), b"")

    def test_existing_socket_path_is_never_overwritten(self):
        Path(self.socket_path).write_text("do not delete")
        process = self.launch("server")
        out, _ = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(out, b"")
        self.assertEqual(Path(self.socket_path).read_text(), "do not delete")

    def test_framing_rejects_malformed_and_unbounded_json(self):
        for raw in (b"[]\n", b"{bad}\n", b'{"v":NaN}\n', b'{"v":1}', b"\xff\n"):
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                read_frame(io.BytesIO(raw))
        with self.assertRaises(ProtocolError):
            write_frame(io.BytesIO(), {"v": "x" * MAX_FRAME_BYTES})

    def test_invalid_request_ids_never_reach_host(self):
        server = self.server()
        client = self.launch("client")
        out, _ = client.communicate(b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n', timeout=5)
        self.assertEqual((client.returncode, out), (1, b""))
        self.assertEqual(server.wait(timeout=5), 0)
        self.assertEqual(server.stdout.read(), b"")


class NativeConfigTest(unittest.TestCase):
    def test_catalog_changes_only_tool_fields_and_rejects_unpinned_sources(self):
        original = {"models": [{"slug": "fixture-model", "model_messages": {"instructions_template": "Original native instructions"},
                                "context_window": 272000, "tool_mode": "code_mode_only", "apply_patch_tool_type": "freeform"}]}
        raw = json.dumps(original).encode()
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            restricted_codex_catalog(raw)
        with patch("experiments.native_config.CODEX_CATALOG_SHA256", hashlib.sha256(raw).hexdigest()):
            restricted = restricted_codex_catalog(raw)
        self.assertEqual(restricted, {"models": [{**original["models"][0], **CODEX_TOOL_METADATA}]})
        self.assertEqual(original["models"][0]["tool_mode"], "code_mode_only")

    def test_no_destination_or_auth_settings_are_accepted(self):
        self.assertEqual(claude_mcp_config()["mcpServers"]["pilot"], {
            "type": "stdio", "command": "python3", "args": ["/opt/pilot/native_relay.py", "client"]})
        argv = client_argv("claude", "fixture-model", "high")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertNotIn("--bare", argv)
        self.assertNotIn("--safe-mode", argv)
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        codex = client_argv("codex", "fixture-model")
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config"):
            self.assertIn(flag, codex)
        self.assertIn("features.shell_tool=false", codex)
        self.assertIn("features.multi_agent=false", codex)
        self.assertIn('model_catalog_json="/opt/pilot/codex-models.json"', codex)
        self.assertIn("tools.experimental_request_user_input.enabled=false", codex)
        self.assertTrue(any('tools={execute={approval_mode="approve"}}' in value for value in codex))
        self.assertIn('approval_policy="never"', codex)
        self.assertIn('sandbox_mode="workspace-write"', codex)
        self.assertNotIn('sandbox_mode="read-only"', codex)
        for model in ("", "--some-option", 'model"\nconfig=bad', None):
            with self.assertRaises(ValueError):
                client_argv("codex", model)


if __name__ == "__main__":
    unittest.main()
