"""Offline-ready MCP protocol bridge to a caller-owned isolated execute callback.

No subprocess, filesystem, credentials, HTTP endpoint, or container-selection
surface is exposed here. native_session connects official containerized clients;
native.py keeps the unintegrated batch route disabled. The callback must enforce command
timeouts and kill descendants (DockerSandbox.execute does so).
"""
from __future__ import annotations

import copy
import json
import math
import time

from .providers import EXECUTE_SCHEMA, TOOL_DESCRIPTION, redact


class ExecuteBridge:
    def __init__(self, execute, record, *, max_calls=30, wall_timeout_seconds=1200,
                 tool_timeout_seconds=120, max_output_chars=32000, clock=time.monotonic):
        for value in (max_calls, wall_timeout_seconds, tool_timeout_seconds, max_output_chars):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Bridge limits must be positive and finite")
        if not isinstance(max_calls, int) or not isinstance(max_output_chars, int):
            raise ValueError("max_calls and max_output_chars must be integers")
        self.execute, self.record, self.clock = execute, record, clock
        self.max_calls, self.tool_limit, self.output_limit = max_calls, tool_timeout_seconds, max_output_chars
        self.deadline = clock() + wall_timeout_seconds
        self.calls, self.seen_ids = 0, set()
        self.initialized = False

    def handle(self, request):
        """Handle one decoded JSON-RPC request; caller controls transport framing."""
        request_id = request.get("id") if isinstance(request, dict) else None

        def fail(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return fail(-32600, "Invalid JSON-RPC request")
        if isinstance(request_id, bool) or (request_id is not None and not isinstance(request_id, (str, int))):
            request_id = None
            return fail(-32600, "Invalid request id")
        method, params = request.get("method"), request.get("params", {})
        if request_id is None:
            return None  # Notifications never execute code.
        if request_id in self.seen_ids:
            return fail(-32600, "Duplicate request id; execution will not be repeated")
        self.seen_ids.add(request_id)
        if not isinstance(params, dict):
            return fail(-32602, "params must be an object")
        if method == "initialize":
            self.initialized = True
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "change-pilots-execute", "version": "1.0"}}
        elif not self.initialized:
            return fail(-32000, "Initialize first")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": "execute", "description": TOOL_DESCRIPTION,
                                 "inputSchema": copy.deepcopy(EXECUTE_SCHEMA)}]}
        elif method in {"resources/list", "resources/templates/list", "prompts/list"}:
            result = {{"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]: []}
        elif method == "tools/call":
            args = params.get("arguments")
            if params.get("name") != "execute" or not isinstance(args, dict) or set(args) != {"command", "timeout_seconds"}:
                return fail(-32602, "Only execute(command, timeout_seconds) is available")
            timeout = args["timeout_seconds"]
            if not isinstance(args["command"], str) or not args["command"] or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                return fail(-32602, "Invalid execute arguments")
            remaining = self.deadline - self.clock()
            if self.calls >= self.max_calls or remaining <= 0:
                return fail(-32000, "Execute budget exhausted")
            self.calls += 1
            timeout = min(timeout, self.tool_limit, remaining)
            self.record(redact({"event": "tool_call", "call_id": request_id,
                                "command": args["command"], "timeout_seconds": timeout}))
            # JSON strings can contain NUL; subprocess argv cannot. Reject it here,
            # before the callback can classify Popen's ValueError as infrastructure.
            # Do not catch arbitrary callback ValueErrors: those may be real bugs.
            if "\0" in args["command"]:
                self.record({"event": "tool_rejected", "call_id": request_id,
                             "reason": "command_contains_nul"})
                result = {
                    "content": [{"type": "text", "text":
                        "Invalid execute command: literal NUL bytes cannot be passed to a process. "
                        "Use an escaped representation in the command text."}], "isError": True}
                self.record({"event": "tool_result", "call_id": request_id, "result": result})
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            try:
                output = json.dumps(self.execute(args["command"], timeout), ensure_ascii=False)
                if len(output) > self.output_limit:
                    output = output[:self.output_limit] + "\n[tool output truncated]"
                result = {"content": [{"type": "text", "text": output}], "isError": False}
            except Exception as exc:
                result = {"content": [{"type": "text", "text": "Isolated execute failed: " + type(exc).__name__}], "isError": True}
            self.record(redact({"event": "tool_result", "call_id": request_id, "result": result}))
        else:
            return fail(-32601, "Method not available")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
