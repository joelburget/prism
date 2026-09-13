"""Add the official Codex patch helper and existing npm's npx to task images.

The context contains only NativeTaskDockerfile. Both local inputs are resolved
by immutable image ID, then given build-specific tags. No package downloads,
authentication, native client configuration, or evaluator files enter the image.
The complete public Codex binary is present because its argv[0] selects patch
mode; task-container isolation still blocks network, credentials, and host files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from .native_setup import docker, image_id, DEFAULT_IMAGE as NATIVE_IMAGE
from .build_image import DEFAULT_IMAGE as TOOLCHAIN_IMAGE

DEFAULT_IMAGE = "prism-change-pilots:0.22.0-native-tools-v2"


def prepare_context(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(Path(__file__).with_name("NativeTaskDockerfile"),
                    destination / "NativeTaskDockerfile")


def build_image(image: str = DEFAULT_IMAGE) -> dict:
    inputs = {"toolchain": image_id(TOOLCHAIN_IMAGE), "native": image_id(NATIVE_IMAGE)}
    tags = {name: f"prism-native-task-input-{uuid.uuid4().hex}:{name}" for name in inputs}
    try:
        for name, expected in inputs.items():
            if image_id(expected) != expected:
                raise ValueError(f"{name} image does not match pinned identity")
            docker("tag", expected, tags[name])
        with tempfile.TemporaryDirectory(prefix="prism-native-task-build-") as temporary:
            context = Path(temporary) / "context"
            prepare_context(context)
            docker("build", "--pull=false", "--network=none", "-f",
                   str(context / "NativeTaskDockerfile"), "--build-arg",
                   f"TOOLCHAIN_IMAGE={tags['toolchain']}", "--build-arg",
                   f"NATIVE_IMAGE={tags['native']}", "--label",
                   f"prism.task.toolchain-image={inputs['toolchain']}", "--label",
                   f"prism.task.patch-source-image={inputs['native']}",
                   "--label", "prism.task.patch-version=0.154.0", "-t", image,
                   str(context), timeout=600)
        for name, expected in inputs.items():
            if image_id(tags[name]) != expected:
                raise ValueError(f"{name} build input changed")
        return {"image": image, "image_id": image_id(image), "inputs": inputs,
                "patch_version": "0.154.0", "auth_requests": 0, "inference_requests": 0}
    finally:
        for tag in tags.values():
            docker("image", "rm", tag, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    print(json.dumps(build_image(args.image), indent=2))


if __name__ == "__main__":
    main()
