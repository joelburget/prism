"""Bounded stdio MCP relay, copied alone into the native-client container.

``server`` bridges one local Unix connection to its controlling host's stdio.
The host feeds requests to ExecuteBridge.handle and writes non-None responses.
``client`` is the native CLI's MCP stdio command. Neither role runs commands,
reads credentials, accepts destination arguments, or opens an Internet socket.
The controlling process owns startup readiness, wall timeout, and teardown.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

SOCKET_PATH = "/tmp/pilot-relay.sock"
MAX_FRAME_BYTES = 1024 * 1024
MAX_MESSAGES = 10000
MAX_ID_CHARS = 256
RESPONSE_TIMEOUT_SECONDS = 180
READY_MARKER = "pilot-relay-ready"


class ProtocolError(ValueError):
    pass


def _reject_constant(_):
    raise ValueError("Nonfinite JSON number")


def read_frame(stream):
    """Read a complete bounded JSON object. EOF is distinct from bad framing."""
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if not raw:
        return None
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ProtocolError("MCP frame exceeds limit or lacks newline")
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON MCP frame") from exc
    if not isinstance(value, dict):
        raise ProtocolError("MCP frame must be an object")
    return value


def write_frame(stream, value):
    try:
        data = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid outgoing MCP frame") from exc
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("Outgoing MCP frame exceeds limit")
    stream.write(data)
    stream.flush()


def is_request(value):
    if value.get("jsonrpc") != "2.0" or not isinstance(value.get("method"), str):
        raise ProtocolError("Invalid JSON-RPC request")
    if "id" not in value:
        return False
    if isinstance(value["id"], bool) or not isinstance(value["id"], (str, int)):
        raise ProtocolError("Invalid JSON-RPC request id")
    if ((isinstance(value["id"], str) and len(value["id"]) > MAX_ID_CHARS)
            or (isinstance(value["id"], int) and abs(value["id"]) > 2**53 - 1)):
        raise ProtocolError("JSON-RPC request id exceeds limit")
    return True


def validate_response(response, request):
    if (response is None or response.get("jsonrpc") != "2.0"
            or type(response.get("id")) is not type(request["id"])
            or response.get("id") != request["id"]
            or ("result" in response) == ("error" in response)
            or "method" in response):
        raise ProtocolError("Mismatched JSON-RPC response")


def _transfer(source, destination, responses, output):
    """Serialize requests; notifications never consume a response frame."""
    messages = 0
    while (request := read_frame(source)) is not None:
        messages += 1
        if messages > MAX_MESSAGES:
            raise ProtocolError("MCP message budget exceeded")
        needs_response = is_request(request)
        write_frame(destination, request)
        if needs_response:
            response = read_frame(responses)
            validate_response(response, request)
            write_frame(output, response)


def run_server():
    # Refuse to overwrite an existing socket, file, or symlink. A new isolated
    # client container is required for every run, so stale sockets are errors.
    bound = False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            old_mask = os.umask(0o177)
            try:
                listener.bind(SOCKET_PATH)
                bound = True
            finally:
                os.umask(old_mask)
            listener.listen(1)
            print(READY_MARKER, file=sys.stderr, flush=True)
            connection, _ = listener.accept()
            with connection, connection.makefile("rwb", buffering=65536) as peer:
                _transfer(peer, sys.stdout.buffer, sys.stdin.buffer, peer)
        finally:
            if bound:
                os.unlink(SOCKET_PATH)


def run_client():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(RESPONSE_TIMEOUT_SECONDS)
        connection.connect(SOCKET_PATH)
        with connection.makefile("rwb", buffering=65536) as peer:
            _transfer(sys.stdin.buffer, peer, peer, sys.stdout.buffer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("server", "client"))
    args = parser.parse_args(argv)
    try:
        (run_server if args.role == "server" else run_client)()
    except (OSError, ProtocolError) as exc:
        # Never include untrusted request text, response text, or OS paths.
        print("pilot-relay-failed: " + type(exc).__name__, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
