"""Provider egress and whole-client construction tests; no account access."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.native_proxy import ALLOWED_HOSTS, connect_target, handle_client, public_connection
from experiments.native_setup import NativeClient, create_client


class TargetTests(unittest.TestCase):
    def test_exact_provider_hosts_only(self):
        for provider, hosts in ALLOWED_HOSTS.items():
            for host in hosts:
                self.assertEqual(connect_target(f"CONNECT {host}:443 HTTP/1.1".encode(), hosts), host)
        for target in ("github.com:443", "1.1.1.1:443", "[::1]:443", "api.openai.com:80",
                       "api.openai.com:0443", "api.openai.com.evil.test:443", "api.openai.com.:443",
                       "user@api.openai.com:443", "https://api.openai.com:443", "api.anthropic.com:443"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                connect_target(f"CONNECT {target} HTTP/1.1".encode(), ALLOWED_HOSTS["openai"])

    def test_non_connect_or_invalid_input_denied(self):
        for line in (b"GET https://api.openai.com/ HTTP/1.1", b"CONNECT api.openai.com:443 HTTP/2",
                     b"CONNECT api.openai.com:443 HTTP/1.1 extra", b"", b"\xff"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                connect_target(line, ALLOWED_HOSTS["openai"])


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_resolution_denied_before_connect(self):
        loop = asyncio.get_running_loop()
        for address in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "fc00::1"):
            with self.subTest(address=address), patch.object(loop, "getaddrinfo", new=AsyncMock(
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])), \
                    patch("asyncio.open_connection", new=AsyncMock()) as connect:
                with self.assertRaises(OSError):
                    await public_connection("api.openai.com")
                connect.assert_not_awaited()

    async def test_denied_requests_never_connect_upstream(self):
        with patch("experiments.native_proxy.public_connection", new=AsyncMock()) as upstream:
            server = await asyncio.start_server(
                lambda r, w: handle_client(r, w, ALLOWED_HOSTS["openai"]), "127.0.0.1", 0)
            async with server:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"CONNECT github.com:443 HTTP/1.1\r\nAuthorization: secret-fixture\r\n\r\n")
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
            self.assertIn(b"403 Forbidden", response)
            self.assertNotIn(b"secret-fixture", response)
            upstream.assert_not_awaited()


class SetupTests(unittest.TestCase):
    def test_offline_probe_has_no_network_or_host_mounts(self):
        calls = []
        def fake(*args, **kwargs):
            calls.append(args)
            output = json.dumps([{"Driver": "local", "Options": None}]) if args[:2] == ("volume", "inspect") else ""
            return subprocess.CompletedProcess(args, 0, output, "")
        with patch("experiments.native_setup.image_id", return_value="sha256:" + "a" * 64), \
                patch("experiments.native_setup.docker", side_effect=fake):
            client = create_client("openai", offline=True, auth_volume="prism-test-auth")
            run = next(call for call in calls if call[0] == "run")
            self.assertEqual(run[run.index("--network")+1], "none")
            self.assertEqual(run[run.index("--user")+1], "1000:1000")
            self.assertIn("--read-only", run)
            self.assertIn("ALL", run)
            self.assertNotIn("--privileged", run)
            self.assertEqual(run[run.index("--mount")+1],
                             "type=volume,source=prism-test-auth,target=/home/native/.codex")
            self.assertFalse(any(call[0] == "network" for call in calls))
            self.assertIsNone(client.proxy_name)
            client.close()
            self.assertFalse(any(call[:2] == ("volume", "rm") for call in calls))

    def test_invalid_provider_and_bind_like_volume_fail_before_docker(self):
        with patch("experiments.native_setup.docker") as run:
            for provider, volume in (("unknown", None), ("openai", "/Users/example/.codex"),
                                     ("openai", "volume,target=/host")):
                with self.assertRaises(ValueError):
                    create_client(provider, auth_volume=volume)
            run.assert_not_called()

    def test_login_is_returned_never_started(self):
        for provider, expected in (("openai", ["codex", "login", "--device-auth"]),
                                   ("anthropic", ["claude", "auth", "login", "--claudeai"])):
            client = NativeClient(provider, "fixture", "image", "digest", "volume")
            with patch("experiments.native_setup.docker") as run:
                self.assertEqual(client.login_argv(), ["docker", "exec", "-it", "fixture", *expected])
                run.assert_not_called()

    def test_named_volume_cannot_hide_a_host_bind(self):
        def fake(*args, **kwargs):
            output = json.dumps([{"Driver": "local", "Options": {
                "type": "none", "device": "/Users/example", "o": "bind"}}])
            return subprocess.CompletedProcess(args, 0, output, "")
        with patch("experiments.native_setup.image_id", return_value="sha256:" + "a" * 64), \
                patch("experiments.native_setup.docker", side_effect=fake) as run:
            with self.assertRaisesRegex(ValueError, "no bind/device/driver options"):
                create_client("openai", offline=True)
            self.assertFalse(any(call.args[0] in {"run", "create"} for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
