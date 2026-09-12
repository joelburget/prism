"""Native controller behavior using bounded in-memory subprocess stand-ins."""
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native_session import RelayPump, run_client, subscription_status


class InputPipe(io.BytesIO):
    def close(self):
        self.written = self.getvalue()
        super().close()


class Process:
    def __init__(self, output=b"", error=b"", returncode=0):
        self.stdin = InputPipe()
        self.stdout = io.BytesIO(output)
        self.stderr = io.BytesIO(error)
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def lines(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


class NativeSessionTest(unittest.TestCase):
    def auth_client(self, provider, status, code=0, stderr=""):
        client = Mock(provider=provider)
        client.exec.return_value = subprocess.CompletedProcess([], code, status, stderr)
        return client

    def test_auth_checks_nonzero_without_raising_or_disclosing_account(self):
        for provider, status in (("openai", "Not logged in: private@example.test"),
                                 ("anthropic", json.dumps({"loggedIn": False, "email": "private@example.test", "access_token": "secret"}))):
            client = self.auth_client(provider, status, 1)
            report = subscription_status(client)
            self.assertFalse(report["authenticated"])
            self.assertNotIn("private", json.dumps(report))
            self.assertNotIn("secret", json.dumps(report))
            self.assertFalse(client.exec.call_args.kwargs["check"])

    def test_subscription_status_rejects_api_auth(self):
        codex = self.auth_client("openai", "Logged in using an API key")
        claude = self.auth_client("anthropic", json.dumps({"loggedIn": True, "authMethod": "api_key"}))
        self.assertFalse(subscription_status(codex)["authenticated"])
        self.assertFalse(subscription_status(claude)["authenticated"])

    def test_subscription_status_discards_account_fields(self):
        client = self.auth_client("anthropic", json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                  "subscriptionType": "max", "email": "private@example.test", "access_token": "secret"}))
        report = subscription_status(client)
        self.assertTrue(report["authenticated"])
        self.assertEqual(report["subscription_type"], "max")
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))

    def test_malformed_auth_status_is_not_authenticated(self):
        for status in ("not json", "[]", "null", '"scalar"'):
            with self.subTest(status=status):
                self.assertFalse(subscription_status(self.auth_client("anthropic", status))["authenticated"])

    def run_fixture(self, process, *, provider="anthropic", wall_seconds=5, relay_error=None):
        client = Mock(provider=provider)
        client.name = "fixture-client"
        client.close.side_effect = lambda: setattr(process, "returncode", -9)
        pump = Mock(error=relay_error)
        events = []
        with patch("experiments.native_session.subprocess.Popen", return_value=process) as popen, \
             patch("experiments.native_session.RelayPump", return_value=pump):
            report = run_client(client, ["fixture-cli", "--json"], "prompt", lambda *_: self.fail("Unexpected tool"),
                                events.append, wall_seconds=wall_seconds)
        return report, events, client, pump, popen

    def test_success_records_usage_and_sends_prompt_only_to_client(self):
        process = Process(lines({"type": "result", "result": "done", "usage": {"input_tokens": 2},
                                 "total_cost_usd": 0.01, "is_error": False}))
        report, events, client, pump, popen = self.run_fixture(process)
        self.assertEqual(report["stop_reason"], "completed")
        self.assertEqual(report["final_text"], "done")
        self.assertEqual(report["usage"], {"input_tokens": 2})
        self.assertIsNone(report["estimated_cost_usd"])
        self.assertEqual(report["api_equivalent_cost_usd"], 0.01)
        self.assertEqual(process.stdin.written, b"prompt")
        self.assertEqual(popen.call_args.args[0], ["docker", "exec", "-i", "fixture-client", "fixture-cli", "--json"])
        pump.close.assert_called_once()
        client.close.assert_not_called()
        self.assertTrue(events)

    def test_codex_final_message_and_usage_are_retained(self):
        report, *_ = self.run_fixture(Process(lines(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "codex done"}},
            {"type": "turn.completed", "usage": {"output_tokens": 3}})), provider="openai")
        self.assertEqual(report["final_text"], "codex done")
        self.assertEqual(report["usage"], {"output_tokens": 3})

    def test_codex_reconnect_followed_by_completion_is_not_fatal(self):
        reconnect = {"type": "error", "message":
            "Reconnecting... 2/5 (stream disconnected before completion: WebSocket protocol error: Connection reset without closing handshake)"}
        complete = {"type": "turn.completed", "usage": {"output_tokens": 3}}
        report, events, *_ = self.run_fixture(Process(lines(reconnect, complete)), provider="openai")
        self.assertEqual(report["stop_reason"], "completed")
        self.assertEqual(report["usage"], {"output_tokens": 3})
        self.assertTrue(any(e.get("payload") == reconnect for e in events))
        # Preserve real failures: no completion, failed turn, unexpected error,
        # error after completion, a newly started unfinished turn, or nonzero exit.
        for stream in [(reconnect,), (reconnect, {"type": "turn.failed"}, complete),
                       ({"type": "error", "message": "unauthorized"}, complete),
                       (complete, reconnect), (complete, {"type": "turn.started"})]:
            with self.subTest(stream=stream):
                report, *_ = self.run_fixture(Process(lines(*stream)), provider="openai")
                self.assertEqual(report["stop_reason"], "native_client_error")
        report, *_ = self.run_fixture(Process(lines(reconnect, complete), returncode=1), provider="openai")
        self.assertEqual(report["stop_reason"], "native_client_error")
        report, *_ = self.run_fixture(Process(lines(reconnect, complete)), provider="openai", relay_error="broken relay")
        self.assertEqual(report["stop_reason"], "native_relay_error")

    def test_returned_metadata_and_final_text_are_redacted(self):
        secret = "sk-offline-fixture-secret-value"
        report, events, *_ = self.run_fixture(Process(lines(
            {"type": "system", "api_key": secret},
            {"type": "result", "result": secret, "is_error": False})))
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, json.dumps(events))

    def test_nonzero_exit_and_native_error_are_reported(self):
        for process in (Process(returncode=2), Process(lines({"type": "result", "is_error": True}))):
            report, *_ = self.run_fixture(process)
            self.assertEqual(report["stop_reason"], "native_client_error")

    def test_unexpected_output_type_does_not_crash(self):
        report, *_ = self.run_fixture(Process(b"[]\nnot-json\n"))
        self.assertIn(report["stop_reason"], {"completed", "native_client_error"})

    def test_wall_timeout_stops_container_and_closes_pump(self):
        report, _, client, pump, _ = self.run_fixture(Process(returncode=None), wall_seconds=0.001)
        self.assertEqual(report["stop_reason"], "native_wall_timeout")
        client.close.assert_called_once()
        pump.close.assert_called_once()

    def test_relay_error_stops_container(self):
        report, _, client, _, _ = self.run_fixture(Process(returncode=None), relay_error="broken relay")
        self.assertEqual(report["stop_reason"], "native_relay_error")
        client.close.assert_called_once()

    def test_output_limit_stops_container(self):
        with patch("experiments.native_session.MAX_LINE", 16):
            report, _, client, _, _ = self.run_fixture(Process(b"x" * 20 + b"\n", returncode=None))
        self.assertEqual(report["stop_reason"], "native_output_limit")
        client.close.assert_called_once()

    def test_blocked_prompt_write_obeys_wall_timeout(self):
        writing, release, finished = threading.Event(), threading.Event(), threading.Event()

        class BlockedInput(InputPipe):
            def write(self, _):
                writing.set()
                release.wait(3)
                raise BrokenPipeError("fixture client stopped")

        process = Process(returncode=None)
        process.stdin = BlockedInput()
        client = Mock(provider="openai")
        client.name = "fixture-client"

        def stop_client():
            process.returncode = -9
            release.set()

        client.close.side_effect = stop_client
        pump = Mock(error=None)
        reports, errors = [], []

        def run():
            try:
                reports.append(run_client(client, ["fixture-cli"], "large prompt", lambda *_: None,
                                          lambda _: None, wall_seconds=0.05))
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        with patch("experiments.native_session.subprocess.Popen", return_value=process), \
             patch("experiments.native_session.RelayPump", return_value=pump):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(writing.wait(1), "Prompt writer never started")
                self.assertTrue(finished.wait(1), "Blocked stdin bypassed the native wall budget")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(errors)
        self.assertEqual(reports[0]["stop_reason"], "native_wall_timeout")
        client.close.assert_called_once()
        pump.close.assert_called_once()


class RelayPumpFailureTest(unittest.TestCase):
    def check_failure(self, *, returncode, error):
        process = Process(error=b"pilot-relay-ready\n" + error, returncode=returncode)
        client = Mock()
        client.name = "fixture-client"
        bridge = Mock()
        pump = RelayPump(client, bridge)
        with patch("experiments.native_session.subprocess.Popen", return_value=process):
            try:
                try:
                    pump.start()
                except RuntimeError:
                    pass  # A failure detected during startup may raise early.
                for thread in pump.threads:
                    thread.join(1)
                self.assertIsNotNone(pump.error, "Relay failure was silently treated as clean EOF")
                bridge.handle.assert_not_called()
            finally:
                pump.close()

    def test_failure_marker_is_reported_even_with_zero_exit(self):
        self.check_failure(returncode=0, error=b"pilot-relay-failed: ProtocolError\n")

    def test_nonzero_relay_exit_without_marker_is_reported(self):
        self.check_failure(returncode=1, error=b"")


if __name__ == "__main__":
    unittest.main()
