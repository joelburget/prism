"""Evaluator-side starter manifests and explicit source allowlists."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath

from run import ROOT, TASKS, ContractError, parse_json

LANGUAGES = ("prism", "python", "typescript")


@dataclass(frozen=True)
class Starter:
    task: str
    language: str
    directory: Path
    entrypoint: str
    build: str | None
    files: tuple[str, ...]

    def source_files(self):
        return [(name, self.directory / name) for name in ("starter.json", *self.files)]

    def digest(self):
        hashes = {name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "executable": bool(path.stat().st_mode & 0o111)}
                  for name, path in self.source_files()}
        return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def load_starter(task: str, language: str, root: Path = ROOT) -> Starter:
    if task not in TASKS or language not in LANGUAGES:
        raise ContractError("unknown starter task or language")
    directory = root / "starters" / task / language
    manifest = directory / "starter.json"
    for path in (root / "starters", root / "starters" / task, directory, manifest):
        if path.is_symlink():
            raise ContractError(f"{path}: starter directories and manifest must not be symlinks")
    try:
        doc = parse_json(manifest.read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError(f"{manifest}: {exc}") from exc
    fields = {"schema_version", "task", "language", "entrypoint", "build", "files"}
    if not isinstance(doc, dict) or set(doc) != fields:
        raise ContractError(f"{manifest}: expected exactly {sorted(fields)}")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise ContractError(f"{manifest}: unsupported schema version")
    if doc["task"] != task or doc["language"] != language:
        raise ContractError(f"{manifest}: task/language mismatch")
    files = doc["files"]
    if not isinstance(files, list) or not files or not all(isinstance(name, str) for name in files):
        raise ContractError(f"{manifest}: files must be a nonempty string array")
    if len(set(files)) != len(files) or "starter.json" in files:
        raise ContractError(f"{manifest}: duplicate or recursive manifest entry")
    forbidden = {".git", "heldout", "node_modules", ".build", "__pycache__"}
    for name in files:
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or "\\" in name or name != relative.as_posix() or any(
            part in {"..", "."} | forbidden for part in relative.parts
        ):
            raise ContractError(f"{manifest}: invalid source path {name!r}")
        path = directory / name
        if not path.is_file() or path.is_symlink() or directory.resolve() not in path.resolve().parents:
            raise ContractError(f"{manifest}: source must be a regular file inside the starter: {name}")
        if any(parent.is_symlink() for parent in path.parents if parent != directory and directory in parent.parents):
            raise ContractError(f"{manifest}: source parent must not be a symlink: {name}")
    if doc["entrypoint"] not in files or (doc["build"] is not None and doc["build"] not in files):
        raise ContractError(f"{manifest}: entrypoint/build must be in the source allowlist")
    if not isinstance(doc["entrypoint"], str) or (doc["build"] is not None and not isinstance(doc["build"], str)):
        raise ContractError(f"{manifest}: invalid entrypoint/build types")
    for name in (doc["entrypoint"], doc["build"]):
        if name is not None and not (directory / name).stat().st_mode & 0o111:
            raise ContractError(f"{manifest}: launcher/build script must be executable: {name}")
    return Starter(task, language, directory, doc["entrypoint"], doc["build"], tuple(files))
