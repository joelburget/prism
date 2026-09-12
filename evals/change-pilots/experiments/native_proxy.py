"""A deliberately small HTTPS CONNECT allowlist; TLS stays end to end.

No access log, headers, tunnel bytes, or provider responses are recorded. This
process runs in its own container, never in the client or on the host.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket


ALLOWED_HOSTS = {
    "openai": frozenset({"chatgpt.com", "auth.openai.com", "api.openai.com"}),
    "anthropic": frozenset({"api.anthropic.com", "claude.ai", "claude.com",
                            "platform.claude.com", "console.anthropic.com"}),
}
MAX_HEADER_BYTES = 16384


def connect_target(request_line: bytes, allowed_hosts: frozenset[str]) -> str:
    """Reject URLs, IP literals, suffix matches, extra ports and non-CONNECT."""
    try:
        method, authority, protocol = request_line.decode("ascii").split()
        host, port = authority.split(":")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid CONNECT target") from None
    if (method != "CONNECT" or protocol not in {"HTTP/1.0", "HTTP/1.1"}
            or port != "443" or host.lower() not in allowed_hosts):
        raise ValueError("denied CONNECT target")
    return host.lower()


async def public_connection(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Resolve once and connect to the validated numeric address (no DNS rebind)."""
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise OSError("non-public destination")
    for family, _, _, _, address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(
                address[0], 443, family=family), timeout=10)
        except OSError:
            pass
    raise OSError("destination unavailable")


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        allowed_hosts: frozenset[str]) -> None:
    upstream_writer = None
    connected = False
    tasks = []
    try:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("oversized header")
            host = connect_target(header.split(b"\r\n", 1)[0], allowed_hosts)
        except (ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        # The CONNECT headers are intentionally discarded, never forwarded/logged.
        del header
        upstream_reader, upstream_writer = await public_connection(host)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        connected = True
        tasks = [asyncio.create_task(relay(reader, upstream_writer)),
                 asyncio.create_task(relay(upstream_reader, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, TimeoutError):
        if not connected:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if upstream_writer:
            upstream_writer.close()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def serve(provider: str, port: int = 8080) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, ALLOWED_HOSTS[provider]),
        "0.0.0.0", port, limit=MAX_HEADER_BYTES)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=ALLOWED_HOSTS)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    asyncio.run(serve(args.provider, args.port))
