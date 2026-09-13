"""Offline native CLI inventory fixture; never authenticates or calls a provider.

Run this module only inside a disposable, network-disabled native client with
an empty HOME. A loopback HTTP fixture supplies canned Responses/Messages SSE;
the normal fixed MCP relay still reaches the controller's DockerSandbox.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANARY = "native-offline-canary-ok"
CANARY_COMMAND = ("test ! -e /Users/joel && test ! -e /evaluator && "
                  "test ! -S /var/run/docker.sock && "
                  "printf 'native-offline-canary-ok\\n' > /work/native-offline-canary && "
                  "cat /work/native-offline-canary")
SENTINEL_KEY = "offline-fixture-not-a-real-key"
BENIGN_CODEX_TOOLS = frozenset({"update_plan", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})
BENIGN_CODEX_TOOLS |= frozenset("functions." + name for name in BENIGN_CODEX_TOOLS)


def tool_names(tools):
    """Include nested namespaces and non-function builtins in the inventory."""
    names = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Malformed provider tool")
        if tool.get("type") == "namespace":
            prefix = tool.get("name", "")
            names.extend(prefix + "." + name for name in tool_names(tool.get("tools", [])))
        else:
            names.append(tool.get("name") or tool.get("function", {}).get("name") or tool.get("type") or "<unnamed>")
    return names


def request_tools(body):
    """Responses Lite puts declarations in input.additional_tools messages."""
    declarations = list(body.get("tools", []))
    for item in body.get("input", []):
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            declarations.extend(item.get("tools", []))
    return declarations


def inventory_report(client, names):
    names = sorted(set(names))
    execute = [name for name in names if name in {"mcp__pilot__execute", "mcp__pilot.execute"}]
    benign = BENIGN_CODEX_TOOLS if client == "codex" else frozenset()
    unexpected = [name for name in names if name not in benign and name not in execute]
    return {"tool_names": names, "execute_tools": execute, "unexpected_tools": unexpected,
            "benign_tools": sorted(set(names) & benign), "inventory_ok": len(execute) == 1 and not unexpected}


def _event(kind, **fields):
    return {"type": kind, **fields}


def responses_events(model, execute_name=None):
    call = execute_name is not None
    namespace, function_name = execute_name.rsplit(".", 1) if call and "." in execute_name else (None, execute_name)
    item = ({"type": "function_call", "id": "fc_offline", "call_id": "call_offline", "name": function_name, **({"namespace": namespace} if namespace else {}),
             "arguments": json.dumps({"command": CANARY_COMMAND, "timeout_seconds": 10}), "status": "completed"}
            if call else {"type": "message", "id": "msg_offline", "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": "Offline probe complete.", "annotations": []}]})
    response = {"id": "resp_offline_call" if call else "resp_offline_done", "object": "response", "created_at": 0,
                "model": model, "status": "in_progress", "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    events = [_event("response.created", response=response),
              _event("response.output_item.added", output_index=0, item={**item, "status": "in_progress", **({"arguments": ""} if call else {"content": []})})]
    if call:
        events.extend([_event("response.function_call_arguments.delta", item_id=item["id"], output_index=0, delta=item["arguments"]),
                       _event("response.function_call_arguments.done", item_id=item["id"], output_index=0, arguments=item["arguments"])])
    else:
        events.extend([_event("response.content_part.added", item_id=item["id"], output_index=0, content_index=0,
                              part={"type": "output_text", "text": "", "annotations": []}),
                       _event("response.output_text.delta", item_id=item["id"], output_index=0, content_index=0,
                              delta="Offline probe complete."),
                       _event("response.output_text.done", item_id=item["id"], output_index=0, content_index=0,
                              text="Offline probe complete.")])
    events.extend([_event("response.output_item.done", output_index=0, item=item),
                   _event("response.completed", response={**response, "status": "completed", "output": [item]})])
    return events


def messages_events(model, execute_name=None):
    call = execute_name is not None
    message = {"id": "msg_offline", "type": "message", "role": "assistant", "model": model,
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 0}}
    block = ({"type": "tool_use", "id": "toolu_offline", "name": execute_name, "input": {}}
             if call else {"type": "text", "text": ""})
    delta = ({"type": "input_json_delta", "partial_json": json.dumps({"command": CANARY_COMMAND, "timeout_seconds": 10})}
             if call else {"type": "text_delta", "text": "Offline probe complete."})
    return [_event("message_start", message=message), _event("content_block_start", index=0, content_block=block),
            _event("content_block_delta", index=0, delta=delta), _event("content_block_stop", index=0),
            _event("message_delta", delta={"stop_reason": "tool_use" if call else "end_turn", "stop_sequence": None},
                   usage={"output_tokens": 1}), _event("message_stop")]


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, client):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.client = client
        self.requests = []
        self.tool_call_issued = False
        self.lock = threading.Lock()


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never log headers or full request bodies.

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                raise ValueError("Invalid body length")
            body = json.loads(self.rfile.read(length))
            names = tool_names(request_tools(body))
        except (ValueError, TypeError):
            self.send_error(400)
            return
        if "count_tokens" in self.path:
            data = b'{"input_tokens":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        protocol = "responses" if self.path.rstrip("/").endswith("/responses") else "messages" if self.path.split("?", 1)[0].endswith("/messages") else None
        if not protocol:
            self.send_error(404)
            return
        with self.server.lock:
            report = inventory_report(self.server.client, names)
            self.server.requests.append({"path": self.path, "protocol": protocol,
                "request_shape": {key: {"type": type(value).__name__,
                    "count": len(value) if isinstance(value, (dict, list)) else None}
                    for key, value in body.items()}, **report})
            execute = report["execute_tools"][0] if report["inventory_ok"] and not self.server.tool_call_issued else None
            if execute:
                self.server.tool_call_issued = True
        events = (responses_events if protocol == "responses" else messages_events)(body.get("model", "offline-model"), execute)
        data = b"".join(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode() for event in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def run_inside(client, model_id, timeout=60, effort=None):
    if not Path("/.dockerenv").is_file():
        raise ValueError("Native fixture requires a Docker container")
    active_interfaces = {p.name for p in Path("/sys/class/net").iterdir()
                         if (p / "flags").is_file() and int((p / "flags").read_text(), 16) & 1}
    if active_interfaces != {"lo"}:
        raise ValueError("Native fixture requires a Docker container with loopback networking only")
    try:
        from .native_config import client_argv
    except ImportError:
        from native_config import client_argv
    # Never inherit any credential, provider routing, proxy, or customization.
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp/probe-home",
           "CODEX_HOME": "/tmp/probe-home/.codex", "CLAUDE_CONFIG_DIR": "/tmp/probe-home/.claude",
           "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DO_NOT_TRACK": "1",
           "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=False)
    Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=False)
    version = subprocess.run([client, "--version"], env=env, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    fixture = FixtureServer(client)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:" + str(fixture.server_port)
    argv = client_argv(client, model_id, effort=effort, max_turns=3)
    if client == "codex":
        argv += ["-c", 'model_provider="offline_fixture"', "-c", 'model_providers.offline_fixture.name="Offline fixture"',
                 "-c", 'model_providers.offline_fixture.base_url="' + base_url + '/v1"',
                 "-c", 'model_providers.offline_fixture.wire_api="responses"',
                 "-c", "model_providers.offline_fixture.requires_openai_auth=false",
                 "-c", "model_providers.offline_fixture.supports_websockets=false"]
    else:
        env.update(ANTHROPIC_BASE_URL=base_url, ANTHROPIC_API_KEY=SENTINEL_KEY)
    try:
        result = subprocess.run(argv, input="Use the execute MCP tool to create the requested harmless offline canary, then finish.",
                                env=env, capture_output=True, text=True, timeout=timeout, check=False)
    finally:
        fixture.shutdown()
        fixture.server_close()
    names = [name for request in fixture.requests for name in request["tool_names"]]
    return {"client": client, "cli_version": version, "model_id": model_id, "effort": effort, "fixture_requests": fixture.requests,
            **inventory_report(client, names), "tool_call_issued": fixture.tool_call_issued,
            "protocol": sorted({r["protocol"] for r in fixture.requests}), "exit_code": result.returncode,
            "stdout": result.stdout[-16000:], "stderr": result.stderr[-8000:],
            "auth_status": "not_checked", "real_inference_requests": 0, "canary_expected": CANARY}


def run_offline_probe(client, model_id, *, effort=None, task_image="prism-change-pilots:0.22.0-py3.14.7-node25.2.1", timeout=90):
    """Controller-side end-to-end proof using a real disposable task container.

    The caller owns a fresh offline client and its blank auth volume. Refuse
    network-enabled clients before starting either the fixture or native CLI.
    """
    from .native_bridge import ExecuteBridge
    from .native_session import RelayPump
    from .sandbox import DockerSandbox
    inspected = subprocess.run(["docker", "inspect", client.name], capture_output=True,
                               text=True, timeout=10, check=True)
    details = json.loads(inspected.stdout)[0]
    if details["HostConfig"]["NetworkMode"] != "none":
        raise ValueError("Offline probe requires a network-disabled client")
    events = []
    with tempfile.TemporaryDirectory(prefix="native-probe-task-") as directory:
        Path(directory, "README.md").write_text("Disposable offline probe canary.\n")
        with DockerSandbox(task_image, Path(directory)) as sandbox:
            bridge = ExecuteBridge(sandbox.execute, events.append, max_calls=2,
                                   wall_timeout_seconds=timeout, tool_timeout_seconds=10)
            mcp_methods = []
            original_handle = bridge.handle
            def traced_handle(request):
                mcp_methods.append(request.get("method") if isinstance(request, dict) else "<invalid>")
                return original_handle(request)
            bridge.handle = traced_handle
            pump = RelayPump(client, bridge)
            try:
                pump.start()
                probe_argv = ["python3", "/opt/pilot/native_probe.py", "--client",
                              "codex" if client.provider == "openai" else "claude",
                              "--model-id", model_id, "--timeout", str(timeout - 5)]
                if effort is not None:
                    probe_argv.extend(["--effort", effort])
                result = client.exec(probe_argv, timeout=timeout, check=False)
                if result.returncode:
                    return {"verified": False, "error": "fixture_process_failed", "exit_code": result.returncode,
                            "stderr": result.stderr[-8000:], "stdout": result.stdout[-16000:]}
                report = json.loads(result.stdout)
                canary = sandbox.execute("cat /work/native-offline-canary", 5)
                report.update(canary_verified=canary["exit_code"] == 0 and canary["stdout"].strip() == CANARY,
                              mcp_tool_calls=bridge.calls, mcp_methods=mcp_methods, relay_error=pump.error, events=events,
                              network_mode="none", mcp_protocol="2024-11-05", auth_status="not_checked")
                report["verified"] = (report["inventory_ok"] and report["tool_call_issued"]
                                      and report["canary_verified"] and bridge.calls == 1
                                      and report["exit_code"] == 0 and pump.error is None)
                return report
            finally:
                pump.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    args = parser.parse_args(argv)
    print(json.dumps(run_inside(args.client, args.model_id, args.timeout, effort=args.effort)))


if __name__ == "__main__":
    main()
