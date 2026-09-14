"""Run official subscription clients with a host-mediated, isolated execute tool.

The client container holds the official CLI's authentication cache. The separate
task container has neither that cache nor network access. MCP crosses docker-exec
stdio, so the client cannot choose a host endpoint or another task container.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from .native_bridge import ExecuteBridge
from .providers import redact

MAX_LINE = 2 * 1024 * 1024
MAX_TRACE = 32 * 1024 * 1024


def codex_reconnect_notice(event):
    """Recognize the pinned CLI's nonterminal stream-retry notification."""
    message = event.get("message")
    return (event.get("type") == "error" and isinstance(message, str)
            and re.fullmatch(r"Reconnecting\.\.\. [1-9][0-9]*/[1-9][0-9]* \(stream disconnected before completion: .+\)",
                             message, re.DOTALL) is not None)


def codex_completed(events):
    """A reconnect is recovered only by a subsequent successful terminal event.

    Unknown error events, a failed turn, and errors after completion still fail.
    Exit status and host/relay failures are checked separately by run_client.
    """
    completed_at = max((i for i, event in enumerate(events)
                        if event.get("type") == "turn.completed"), default=-1)
    if completed_at < 0:
        return False
    for i, event in enumerate(events):
        kind = event.get("type")
        if kind == "turn.failed" or (kind == "turn.started" and i > completed_at):
            return False
        if kind == "error" and (not codex_reconnect_notice(event) or i > completed_at):
            return False
    return True


def subscription_status(client):
    """Return only auth method/readiness; never return account or token contents."""
    if client.provider == "openai":
        result = client.exec(["codex", "login", "status"], timeout=20, check=False)
        logged_in = result.returncode == 0 and "Logged in using ChatGPT" in result.stdout + result.stderr
        return {"provider": "openai", "authenticated": logged_in,
                "method": "chatgpt" if logged_in else "not_subscription_authenticated"}
    result = client.exec(["claude", "auth", "status"], timeout=20, check=False)
    try:
        status = json.loads(result.stdout)
    except (ValueError, TypeError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    logged_in = result.returncode == 0 and status.get("loggedIn") is True and status.get("authMethod") == "claude.ai"
    return {"provider": "anthropic", "authenticated": logged_in,
            "method": "claude.ai" if logged_in else "not_subscription_authenticated",
            "subscription_type": status.get("subscriptionType") if logged_in else None}


class RelayPump:
    """Translate framed MCP requests from a client to the fixed execute callback."""
    def __init__(self, client, bridge):
        self.client, self.bridge = client, bridge
        self.ready = threading.Event()
        self.error = None
        self.process = None
        self._closing = False

    @property
    def error(self):
        if self._error:
            return self._error
        if not self._closing and self.process is not None and self.process.poll() not in (None, 0):
            return "relay_nonzero_exit"
        return None

    @error.setter
    def error(self, value):
        self._error = value

    def start(self):
        self.process = subprocess.Popen(
            ["docker", "exec", "-i", self.client.name, "python3", "/opt/pilot/native_relay.py", "server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def stderr():
            while line := self.process.stderr.readline(MAX_LINE + 1):
                if line.strip() == b"pilot-relay-ready":
                    self.ready.set()
                if line.startswith(b"pilot-relay-failed:"):
                    self.error = "relay_reported_failure"
                if len(line) > MAX_LINE:
                    self.error = "relay output limit"
                    break

        def respond():
            try:
                while line := self.process.stdout.readline(MAX_LINE + 1):
                    if len(line) > MAX_LINE:
                        raise ValueError("oversized relay request")
                    reply = self.bridge.handle(json.loads(line))
                    if reply is not None:
                        self.process.stdin.write((json.dumps(reply) + "\n").encode())
                        self.process.stdin.flush()
            except Exception as exc:
                self.error = type(exc).__name__

        self.threads = [threading.Thread(target=stderr, daemon=True), threading.Thread(target=respond, daemon=True)]
        for thread in self.threads:
            thread.start()
        if not self.ready.wait(10):
            self.close()
            raise RuntimeError("native MCP relay did not become ready")

    def close(self):
        self._closing = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                close_pipe(stream)


def close_pipe(stream):
    """A stopped peer may break the final buffered flush; cleanup must not hide a result."""
    try:
        stream.close()
    except BrokenPipeError:
        pass


def run_client(client, argv, prompt, execute, record, *, wall_seconds=1800, max_calls=100):
    """Run a preconfigured official client; caller verifies subscription first.

    This is native-harness accounting, not shared-API accounting. Dollar values
    reported by a CLI are API-equivalent estimates, not subscription invoices.
    """
    lock = threading.Lock()
    def emit(event):
        with lock:
            record(redact(event))
    bridge = ExecuteBridge(execute, emit, max_calls=max_calls, wall_timeout_seconds=wall_seconds)
    pump = RelayPump(client, bridge)
    started = time.monotonic()
    events = []
    failure = []
    total_bytes = [0]
    process = None
    try:
        pump.start()
        process = subprocess.Popen(["docker", "exec", "-i", client.name, *argv],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def collect(stream, channel):
            try:
                while line := stream.readline(MAX_LINE + 1):
                    with lock:
                        total_bytes[0] += len(line)
                        exceeded = total_bytes[0] > MAX_TRACE or len(line) > MAX_LINE
                    if exceeded:
                        failure.append("native_output_limit")
                        break
                    if channel == "stdout":
                        try:
                            event = json.loads(line)
                        except ValueError:
                            event = {"type": "unstructured_output", "text": line.decode("utf-8", "replace")}
                        if not isinstance(event, dict):
                            event = {"type": "unexpected_output_type"}
                        events.append(event)
                        emit({"event": "native_event", "payload": event})
                    else:
                        emit({"event": "native_stderr", "text": line.decode("utf-8", "replace")})
            except (OSError, ValueError):
                failure.append("native_stream_error")

        threads = [threading.Thread(target=collect, args=(process.stdout, "stdout"), daemon=True),
                   threading.Thread(target=collect, args=(process.stderr, "stderr"), daemon=True)]
        for thread in threads:
            thread.start()
        def feed_prompt():
            try:
                process.stdin.write(prompt.encode())
                process.stdin.close()
            except (OSError, ValueError):
                failure.append("native_stdin_error")
        writer = threading.Thread(target=feed_prompt, daemon=True)
        writer.start()
        while process.poll() is None:
            expired = time.monotonic() - started >= wall_seconds
            if failure or expired or pump.error:
                failure.append(failure[0] if failure else "native_wall_timeout" if expired else "native_relay_error")
                # Stopping the container also kills descendants of docker-exec.
                client.close()
                break
            time.sleep(.05)
        process.wait(timeout=10)
        writer.join(timeout=5)
        if writer.is_alive():
            failure.append("native_stdin_error")
            client.close()
            writer.join(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        if pump.error and not failure:
            failure.append("native_relay_error")
        last = next((event for event in reversed(events) if event.get("type") == "result"), {})
        usage = last.get("usage", {})
        if client.provider == "openai":
            usage = next((event.get("usage", {}) for event in reversed(events)
                          if event.get("type") == "turn.completed"), {})
        final_text = last.get("result", "")
        if client.provider == "openai":
            final_text = next((event["item"].get("text", "") for event in reversed(events)
                               if event.get("type") == "item.completed"
                               and isinstance(event.get("item"), dict)
                               and event["item"].get("type") == "agent_message"), "")
        completed = bool(last) and not last.get("is_error")
        if client.provider == "openai":
            completed = codex_completed(events)
        stop = failure[0] if failure else "completed" if process.returncode == 0 and completed else "native_client_error"
        return {"harness": "subscription-native", "stop_reason": stop,
                "elapsed_seconds": time.monotonic() - started, "tool_calls": bridge.calls,
                "exit_code": process.returncode, "usage": usage,
                "estimated_cost_usd": None, "api_equivalent_cost_usd": last.get("total_cost_usd"),
                "subscription_usage": "reported_by_native_client", "billing_unknown": False,
                "model_events": redact([event for event in events if event.get("type") in ("system", "thread.started")]),
                "final_text": redact(final_text)}
    finally:
        pump.close()
        if process is not None:
            if process.poll() is None:
                client.close()
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    close_pipe(stream)
