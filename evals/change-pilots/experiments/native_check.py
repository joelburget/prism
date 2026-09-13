"""Reproducible native isolation checks and interactive subscription login.

Checks use a local fake provider, no credentials and no paid inference. Login
runs the official client and persists only its own cache in a named volume.
Run from evals/change-pilots: python3 -m experiments.native_check --help.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from .native_probe import run_offline_probe
from .native_session import subscription_status
from .native_setup import DEFAULT_IMAGE, create_client, docker, image_id, inspect_boundary, verify_network
from .native_task_setup import DEFAULT_IMAGE as TASK_IMAGE


def verify(provider, model_id, image=DEFAULT_IMAGE, effort=None, task_image=TASK_IMAGE):
    volume = "prism-native-check-" + uuid.uuid4().hex
    task_id = image_id(task_image)
    try:
        with create_client(provider, image, auth_volume=volume, offline=True) as client:
            identity = client.image_id
            offline_boundary = inspect_boundary(client)
            inventory = run_offline_probe(client, model_id, effort=effort, task_image=task_id)
        # Pin the same image even if a concurrent build changes the image tag.
        with create_client(provider, identity, auth_volume=volume) as client:
            boundary = inspect_boundary(client)
            network = verify_network(client)
        return {"provider": provider, "model_id": model_id, "effort": effort, "image_id": identity,
                "task_image_id": task_id,
                "offline_boundary": offline_boundary, "boundary": boundary,
                "network": network, "inventory": inventory,
                "isolation_and_routing_verified": all((offline_boundary["passed"],
                    boundary["passed"], network["passed"], inventory["verified"])),
                "subscription_access_verified": False, "real_inference_requests": 0}
    finally:
        docker("volume", "rm", volume, check=False)


def verify_all(image=DEFAULT_IMAGE, task_image=TASK_IMAGE):
    """Refresh the planner receipt using every configured model's offline fixture."""
    from .costs import select_models
    identity, task_id = image_id(image), image_id(task_image)
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "image_id": identity,
              "task_image_id": task_id, "model_checks": [], "boundary_checks": [],
              "real_inference_requests": 0, "subscription_access_verified": False}
    for model in select_models(None):
        effort = model.get("reasoning") if model["provider"] == "openai" else model.get("effort")
        checked = verify(model["provider"], model["model_id"], identity, effort, task_id)
        if not checked["isolation_and_routing_verified"]:
            raise ValueError(f"Offline routing verification failed for {model['key']}")
        report["model_checks"].append(checked["inventory"])
        if not any(b["provider"] == model["provider"] for b in report["boundary_checks"]):
            report["boundary_checks"].append({"provider": model["provider"],
                "container": checked["boundary"], "network": checked["network"]})
        print(f"Verified offline route: {model['key']}", flush=True)
    directory = Path(__file__).resolve().parent
    report["source_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("native_bridge.py", "native_config.py", "native_probe.py", "native_proxy.py",
                     "native_relay.py", "native_session.py", "native_setup.py", "native_check.py",
                     "native_task_setup.py", "sandbox.py", "build_image.py", "Dockerfile", "NativeDockerfile", "NativeTaskDockerfile")}
    return report


def import_codex_cache(client):
    """Documented headless auth-cache copy; never decode, print or export tokens."""
    if client.provider != "openai" or not inspect_boundary(client)["passed"]:
        raise ValueError("Codex cache import requires a verified OpenAI client container")
    with (Path.home() / ".codex" / "auth.json").open("rb") as source:
        result = subprocess.run(["docker", "exec", "-i", client.name, "sh", "-c",
                                 "umask 077; cat > /home/native/.codex/auth.json"],
                                stdin=source, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError("Could not import Codex cache into native auth volume")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "verify-all", "status", "login", "import-codex-login"))
    parser.add_argument("--provider", choices=("openai", "anthropic"))
    parser.add_argument("--model-id")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--task-image", default=TASK_IMAGE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command != "verify-all" and not args.provider:
        parser.error("--provider is required for this command")
    if args.command == "verify-all":
        report = verify_all(args.image, args.task_image)
    elif args.command == "verify":
        if not args.model_id:
            parser.error("verify requires --model-id (tool inventory can depend on the model)")
        report = verify(args.provider, args.model_id, args.image, args.effort, args.task_image)
    else:
        if args.command == "import-codex-login" and args.provider != "openai":
            parser.error("import-codex-login requires --provider openai")
        with create_client(args.provider, args.image) as client:
            if not inspect_boundary(client)["passed"]:
                raise RuntimeError("Native client boundary verification failed")
            if args.command == "login":
                # Interactive browser flow belongs to the official native CLI.
                subprocess.run(client.login_argv(), check=False)
            elif args.command == "import-codex-login":
                import_codex_cache(client)
            report = subscription_status(client)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    if args.command == "verify" and not report["isolation_and_routing_verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
