#!/usr/bin/env python3
"""Build the evaluation toolchain without sending evals, worktrees or Git to Docker."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

PRISM_COMMIT = "2cfe818bc17d91c2a5fb452901cb40b8e8bee564"  # v0.18.0
DEFAULT_IMAGE = "prism-change-pilots:0.18.0-py3.14.7-node25.2.1"
# Deliberately list compiler inputs; never archive the repository root.
COMPILER_INPUTS = ("Cargo.toml", "Cargo.lock", "build.rs", "rust-toolchain.toml",
                   "README.md", "LICENSE", "src", "bin", "crates", "runtime", "lib",
                   "benches", "packages/tc", "docs/src/spec.md", "docs/src/stdlib")


def prepare_context(repository: Path, destination: Path) -> dict:
    if destination.exists():
        raise ValueError("build context destination must not exist")
    data = subprocess.run(["git", "-C", str(repository), "archive", "--format=tar",
                           PRISM_COMMIT, "--", *COMPILER_INPUTS], check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    compiler = destination / "compiler"
    compiler.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or any(p in {".git", "evals"} for p in path.parts):
                raise ValueError(f"unexpected compiler archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"non-regular compiler input: {member.name}")
            target = compiler / path
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    provenance = {"prism_commit": PRISM_COMMIT, "prism_version": "0.18.0",
                  "compiler_archive_sha256": hashlib.sha256(data).hexdigest(),
                  "python_version": "3.14.7", "node_version": "25.2.1",
                  "typescript_version": "5.9.3", "node_types_version": "25.0.3",
                  "rust_version": "1.96.0", "llvm_major": 22,
                  "reproducibility": "Source commit and language versions pinned; Debian/LLVM apt packages and base-image tags are not digest-pinned."}
    (destination / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    shutil.copyfile(Path(__file__).with_name("Dockerfile"), destination / "Dockerfile")
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--context", type=Path, help="only prepare a new inspectable build context")
    args = parser.parse_args()
    if args.context:
        print(json.dumps(prepare_context(args.repository, args.context), indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="prism-image-") as temporary:
            context = Path(temporary) / "context"
            prepare_context(args.repository, context)
            subprocess.run(["docker", "build", "--tag", args.image, str(context)], check=True)
        print(f"Built {args.image}; record its immutable image ID in each experiment.")


if __name__ == "__main__":
    main()
