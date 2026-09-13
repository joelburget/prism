"""Build and verify whole-client containers without authenticating or inferring.

Only provider-specific named volumes persist. No host files, homes, sockets or
environment credentials are mounted or copied. Login helpers return commands;
they never start authentication. TLS verification sends no HTTP request.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from .native_proxy import ALLOWED_HOSTS

DEFAULT_IMAGE = "prism-native-clients:prism0.22.0-codex0.154.0-claude2.1.257"
AUTH_DIRS = {"openai": "/home/native/.codex", "anthropic": "/home/native/.claude"}


def docker(*args: str, input: str | None = None, timeout: float = 60,
           check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=input, text=True,
                          capture_output=True, timeout=timeout, check=check)


def build_image(image: str = DEFAULT_IMAGE) -> dict:
    """Use an allowlisted build context so repository/host auth cannot enter it."""
    directory = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="prism-native-build-") as temporary:
        for name in ("NativeDockerfile", "native_proxy.py", "native_relay.py", "native_config.py", "native_probe.py"):
            shutil.copyfile(directory / name, Path(temporary) / name)
        docker("build", "--pull=false", "-f", str(Path(temporary) / "NativeDockerfile"),
               "-t", image, temporary, timeout=1200)
    return {"image": image, "image_id": image_id(image),
            "client_versions": {"codex": "0.154.0", "claude": "2.1.257"},
            "auth_requests": 0, "inference_requests": 0}


def image_id(image: str) -> str:
    value = docker("image", "inspect", image, "--format", "{{.Id}}").stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("unexpected Docker image identity")
    return value


def hardened_arguments() -> list[str]:
    return ["--user", "1000:1000", "--cap-drop", "ALL", "--security-opt",
            "no-new-privileges:true", "--read-only", "--pids-limit", "256",
            "--memory", "2g", "--cpus", "2", "--init",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
            "--tmpfs", "/work:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=700",
            "--tmpfs", "/session:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=700",
            "--tmpfs", "/home/native:rw,nosuid,nodev,size=32m,uid=1000,gid=1000,mode=700"]


@dataclass
class NativeClient:
    provider: str
    name: str
    image: str
    image_id: str
    auth_volume: str
    internal_network: str | None = None
    egress_network: str | None = None
    proxy_name: str | None = None
    offline: bool = False

    @property
    def auth_dir(self) -> str:
        return AUTH_DIRS[self.provider]

    def exec(self, argv: list[str], *, input: str | None = None,
             timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
        args = ["exec", *(["-i"] if input is not None else []), self.name, *argv]
        return docker(*args, input=input, timeout=timeout, check=check)

    def login_argv(self) -> list[str]:
        cli = ["codex", "login", "--device-auth"] if self.provider == "openai" else [
            "claude", "auth", "login", "--claudeai"]
        return ["docker", "exec", "-it", self.name, *cli]

    def close(self) -> None:
        # Retain the explicitly named provider credential volume across runs.
        for name in (self.name, self.proxy_name):
            if name:
                docker("rm", "-f", name, check=False)
        for name in (self.internal_network, self.egress_network):
            if name:
                docker("network", "rm", name, check=False)

    def __enter__(self) -> NativeClient:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()


def create_client(provider: str, image: str = DEFAULT_IMAGE, *,
                  auth_volume: str | None = None, offline: bool = False) -> NativeClient:
    if provider not in ALLOWED_HOSTS:
        raise ValueError("provider must be openai or anthropic")
    volume = auth_volume or f"prism-native-auth-{provider}"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", volume):
        raise ValueError("auth_volume must be a Docker named volume")
    prefix = "prism-native-" + uuid.uuid4().hex[:12]
    client = NativeClient(provider, prefix + "-client", image, image_id(image), volume,
                          offline=offline)
    if not offline:
        client.internal_network = prefix + "-internal"
        client.egress_network = prefix + "-egress"
        client.proxy_name = prefix + "-proxy"
    try:
        docker("volume", "create", "--label", "prism.native.auth=" + provider, volume)
        volume_info = json.loads(docker("volume", "inspect", volume).stdout)[0]
        if volume_info.get("Driver") != "local" or volume_info.get("Options"):
            raise ValueError("auth volume must be local with no bind/device/driver options")
        proxy_env = []
        if not offline:
            # No bridge gateway address: even the Docker host is not reachable.
            docker("network", "create", "--internal", "--driver", "bridge", "--opt",
                   "com.docker.network.bridge.gateway_mode_ipv4=isolated", client.internal_network)
            docker("network", "create", "--driver", "bridge", client.egress_network)
            docker("create", "--name", client.proxy_name, "--network", client.egress_network,
                   *hardened_arguments(), client.image_id, "python3", "/opt/native/native_proxy.py",
                   "--provider", provider)
            docker("network", "connect", "--alias", "native-proxy", client.internal_network,
                   client.proxy_name)
            docker("start", client.proxy_name)
            info = json.loads(docker("inspect", client.proxy_name).stdout)[0]
            address = info["NetworkSettings"]["Networks"][client.internal_network]["IPAddress"]
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                proxy_env.extend(["--env", f"{key}=http://{address}:8080"])
            proxy_env.extend(["--env", "NO_PROXY=localhost,127.0.0.1,::1",
                              "--env", "no_proxy=localhost,127.0.0.1,::1"])
        docker("run", "--detach", "--name", client.name, "--network",
               "none" if offline else client.internal_network,
               "--dns", "127.0.0.1", *hardened_arguments(), *proxy_env,
               "--mount", f"type=volume,source={volume},target={client.auth_dir}",
               client.image_id, "sleep", "infinity")
        return client
    except BaseException:
        client.close()
        raise


def inspect_boundary(client: NativeClient) -> dict:
    """Check actual Docker configuration, without inspecting credential files."""
    info = json.loads(docker("inspect", client.name).stdout)[0]
    host = info["HostConfig"]
    expected_network = "none" if client.offline else client.internal_network
    checks = {
        "image_identity": info["Image"] == client.image_id,
        "nonroot": info["Config"]["User"] == "1000:1000",
        "unprivileged": host["Privileged"] is False,
        "capabilities_dropped": host["CapDrop"] == ["ALL"] and not host["CapAdd"],
        "no_new_privileges": "no-new-privileges:true" in host["SecurityOpt"],
        "read_only_root": host["ReadonlyRootfs"] is True,
        "no_host_namespaces": all(host.get(key) != "host" and not str(host.get(key, "")).startswith("container")
                                  for key in ("PidMode", "IpcMode", "UTSMode")),
        "no_host_binds": not host.get("Binds") and all(m["Type"] in {"volume", "tmpfs"}
                                                       for m in info["Mounts"]),
        "only_auth_volume": [(m.get("Name"), m["Destination"]) for m in info["Mounts"]
                             if m["Type"] == "volume"] == [(client.auth_volume, client.auth_dir)],
        "no_devices": not host.get("Devices") and not host.get("DeviceRequests"),
        "no_published_ports": not host.get("PortBindings"),
        "only_expected_network": set(info["NetworkSettings"]["Networks"]) == {expected_network},
        "dns_forwarding_disabled": host["Dns"] == ["127.0.0.1"],
    }
    if not client.offline:
        network = json.loads(docker("network", "inspect", client.internal_network).stdout)[0]
        checks["internal_network"] = network["Internal"] is True
        checks["isolated_gateway"] = network["Options"].get(
            "com.docker.network.bridge.gateway_mode_ipv4") == "isolated"
        proxy = json.loads(docker("inspect", client.proxy_name).stdout)[0]
        checks["proxy_two_networks"] = set(proxy["NetworkSettings"]["Networks"]) == {
            client.internal_network, client.egress_network}
        checks["proxy_no_auth_mounts"] = not any(m["Type"] != "tmpfs" for m in proxy["Mounts"])
        checks["proxy_image_identity"] = proxy["Image"] == client.image_id
        proxy_host = proxy["HostConfig"]
        checks["proxy_hardened"] = (
            proxy["Config"]["User"] == "1000:1000" and proxy_host["ReadonlyRootfs"] is True
            and proxy_host["Privileged"] is False and proxy_host["CapDrop"] == ["ALL"]
            and not proxy_host["CapAdd"] and "no-new-privileges:true" in proxy_host["SecurityOpt"]
            and not proxy_host.get("PortBindings") and not proxy_host.get("Binds")
            and not proxy_host.get("Devices") and not proxy_host.get("DeviceRequests"))
    volume = json.loads(docker("volume", "inspect", client.auth_volume).stdout)[0]
    checks["auth_volume_no_driver_options"] = volume.get("Driver") == "local" and not volume.get("Options")
    return {"passed": all(checks.values()), "checks": checks, "image_id": client.image_id}


def verify_network(client: NativeClient) -> dict:
    """Exercise denial and provider TLS handshakes; no credentials or HTTP body."""
    if client.offline:
        raise ValueError("network verification needs the provider proxy")
    source = r'''
import json, os, socket, ssl
from urllib.parse import urlsplit
p = urlsplit(os.environ['HTTPS_PROXY'])
def connect(authority, method='CONNECT'):
    s = socket.create_connection((p.hostname, p.port), timeout=10)
    s.sendall((method+' '+authority+' HTTP/1.1\r\nHost: '+authority+'\r\n\r\n').encode())
    response = b''
    while not response.endswith(b'\r\n\r\n'):
        data = s.recv(1)
        if not data: break
        response += data
    return s, response.split(b'\r\n')[0]
checks = {}
for label, authority, method in [
    ('github_blocked', 'github.com:443', 'CONNECT'),
    ('ip_literal_blocked', '1.1.1.1:443', 'CONNECT'),
    ('http_blocked', 'http://github.com/', 'GET'),
    ('port_80_blocked', 'AUTH_HOST:80', 'CONNECT'),
    ('suffix_attack_blocked', 'AUTH_HOST.evil.example:443', 'CONNECT'),
    ('cross_provider_blocked', 'OTHER_HOST:443', 'CONNECT')]:
    s, status = connect(authority, method)
    s.close()
    checks[label] = b' 403 ' in status
try:
    s = socket.create_connection(('1.1.1.1', 443), timeout=2)
    s.close()
    checks['direct_egress_blocked'] = False
except OSError:
    checks['direct_egress_blocked'] = True
try:
    socket.getaddrinfo('github.com', 443)
    checks['external_dns_blocked'] = False
except OSError:
    checks['external_dns_blocked'] = True
for host in ALLOWED:
    s, status = connect(host+':443')
    if b' 200 ' not in status:
        checks['tls_'+host] = False
        s.close()
        continue
    try:
        with ssl.create_default_context().wrap_socket(s, server_hostname=host) as tls:
            checks['tls_'+host] = bool(tls.version())
    except OSError:
        checks['tls_'+host] = False
print(json.dumps({'passed': all(checks.values()), 'checks': checks,
                  'auth_requests': 0, 'inference_requests': 0}))
'''
    host = "auth.openai.com" if client.provider == "openai" else "claude.ai"
    other = "api.anthropic.com" if client.provider == "openai" else "api.openai.com"
    source = source.replace("AUTH_HOST", host).replace("OTHER_HOST", other)
    source = source.replace("ALLOWED", repr(sorted(ALLOWED_HOSTS[client.provider])))
    result = client.exec(["python3", "-c", source], timeout=120)
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "verify", "prepare-login"])
    parser.add_argument("--provider", choices=ALLOWED_HOSTS, default="openai")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    if args.command == "build":
        result = build_image(args.image)
    elif args.command == "verify":
        volume = "prism-native-verify-" + uuid.uuid4().hex
        try:
            with create_client(args.provider, args.image, auth_volume=volume) as client:
                result = {"provider": args.provider, "boundary": inspect_boundary(client),
                          "network": verify_network(client)}
        finally:
            docker("volume", "rm", volume, check=False)
    else:
        client = create_client(args.provider, args.image)
        result = {"provider": args.provider, "container": client.name,
                  "auth_volume": client.auth_volume, "login_argv": client.login_argv(),
                  "boundary": inspect_boundary(client), "login_started": False}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
