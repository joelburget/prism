"""Docker isolation for agent shells and disposable, evaluator-only case executions.

Docker is the trust boundary, not a copied host directory. No host bind mounts,
network, credentials, elevated capabilities or Docker socket enter containers.
The daemon and selected image are trusted. This is not a VM security boundary.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

OUTPUT_LIMIT = 1024 * 1024
SOURCE_LIMIT = 16 * 1024 * 1024
ARCHIVE_LIMIT = 64 * 1024 * 1024
FILE_LIMIT = 2048
EXCLUDED = {".build", "__pycache__", "node_modules", ".git", ".pytest_cache"}
SOURCE_SUFFIXES = {".py", ".pr", ".ts", ".json", ".md", ".sh", ".toml", ".txt", ".lock"}


class SandboxError(RuntimeError):
    """Docker/controller infrastructure failure."""


class InvalidSubmissionError(SandboxError):
    """The submitted source violates the artifact contract; score as failure."""


class SubmissionBuildError(SandboxError):
    """Submitted code did not build; distinct from Docker infrastructure errors."""
    def __init__(self, build_result):
        self.build_result = build_result
        super().__init__(f"evaluation build failed: {json.dumps(build_result)}")


class SandboxFrozenError(SandboxError):
    """A prior timeout/output overflow froze the agent's execution environment."""


def _capture(argv, timeout_seconds=60, stdin=b"", output_limit=OUTPUT_LIMIT):
    """Bound both streams in memory; kill the local CLI on timeout/overflow."""
    started = time.monotonic()
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = output_limited = False
    sent = 0
    selector = selectors.DefaultSelector()
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    if stdin:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        process.stdin.close()
    try:
        while selector.get_map():
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == "stdin":
                    try:
                        sent += os.write(key.fd, stdin[sent:sent + 65536])
                    except BrokenPipeError:
                        sent = len(stdin)
                    if sent == len(stdin):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = streams[key.data]
                available = output_limit - len(target)
                target.extend(chunk[:available])
                if len(chunk) > available:
                    output_limited = True
                    break
            if output_limited:
                break
        if timed_out or output_limited:
            process.kill()
        try:
            process.wait(timeout=max(0.1, timeout_seconds - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    try:
        bytes(streams["stdout"]).decode("utf-8", "strict")
        stdout_invalid_utf8 = False
    except UnicodeDecodeError:
        stdout_invalid_utf8 = True
    return {"exit_code": process.returncode, "stdout_invalid_utf8": stdout_invalid_utf8,
            "stdout": bytes(streams["stdout"]).decode("utf-8", "replace"),
            "stderr": bytes(streams["stderr"]).decode("utf-8", "replace"),
            "elapsed_seconds": time.monotonic() - started, "timed_out": timed_out,
            "output_limited": output_limited}


def _docker(*args, timeout=60):
    result = _capture(["docker", *map(str, args)], timeout_seconds=timeout)
    if result["exit_code"] or result["timed_out"] or result["output_limited"]:
        raise SandboxError(f"docker {args[0]} failed: {result['stderr'] or result['stdout']}")
    return result["stdout"].strip()


def image_identity(image: str) -> dict:
    """Resolve a local image reference once; execution never implicitly pulls."""
    record = json.loads(_docker("image", "inspect", image))[0]
    if record.get("Config", {}).get("Volumes"):
        raise SandboxError("sandbox images must not declare volumes")
    return {"id": record["Id"], "repo_digests": record.get("RepoDigests", []),
            "architecture": record.get("Architecture"), "os": record.get("Os")}


def _limits():
    # Prism 0.22 native linking opens more stdlib objects than the old 256-FD cap allows.
    return ["--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "2g", "--memory-swap", "2g", "--cpus", "2",
            "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=67108864:67108864",
            "--user", "1000:1000", "--workdir", "/work", "--env", "HOME=/tmp/home",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "/bin/sh"]


def _regular_tree(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise InvalidSubmissionError("bundle/source must be a regular directory")
    total = count = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise InvalidSubmissionError(f"symlinks and special files are forbidden: {path}")
            if stat.S_ISREG(mode):
                count += 1
                total += path.stat().st_size
                if count > FILE_LIMIT or total > SOURCE_LIMIT:
                    raise InvalidSubmissionError("bundle/source exceeds size or file limit")
                yield path.relative_to(root).as_posix(), path


def _upload_tree(container: str, root: Path, prefix=""):
    buffer = io.BytesIO()
    files = list(_regular_tree(root))
    directories = set()
    for name, _ in files:
        relative = PurePosixPath(prefix) / name
        directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != ".")
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted(directories, key=lambda value: (value.count("/"), value)):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.uid = info.gid = 1000
            info.mode = 0o755
            archive.addfile(info)
        for name, path in files:
            info = tarfile.TarInfo(str(PurePosixPath(prefix) / name))
            data = path.read_bytes()
            info.size = len(data)
            info.uid = info.gid = 1000
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            archive.addfile(info, io.BytesIO(data))
    result = _capture(["docker", "cp", "-a", "-", f"{container}:/work"], stdin=buffer.getvalue())
    if result["exit_code"] or result["timed_out"] or result["output_limited"]:
        raise SandboxError(f"bundle upload failed: {result['stderr']}")


def extract_source_archive(archive_path: Path, destination: Path) -> Path:
    """Manually validate and extract docker cp output; never use tar.extractall."""
    if destination.exists() or destination.is_symlink():
        raise SandboxError("snapshot destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".source-", dir=destination.parent))
    total = count = 0
    seen = set()
    try:
        with tarfile.open(archive_path) as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if (relative.is_absolute() or ".." in relative.parts or "\\" in member.name
                        or not relative.parts or relative.parts[0] != "starter"
                        or member.name.rstrip("/") != relative.as_posix()):
                    raise InvalidSubmissionError(f"unsafe snapshot path: {member.name}")
                if not (member.isdir() or member.isfile()):
                    raise InvalidSubmissionError(f"snapshot contains symlink or special file: {member.name}")
                if member.name in seen:
                    raise InvalidSubmissionError(f"duplicate snapshot path: {member.name}")
                seen.add(member.name)
                parts = relative.parts[1:]
                if not parts or any(part in EXCLUDED for part in parts):
                    continue
                target = temporary.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if PurePosixPath(parts[-1]).suffix not in SOURCE_SUFFIXES and parts[-1] != ".gitignore":
                    raise InvalidSubmissionError(f"snapshot file is not an allowed source file: {member.name}")
                count += 1
                total += member.size
                if member.size < 0 or count > FILE_LIMIT or total > SOURCE_LIMIT:
                    raise InvalidSubmissionError("snapshot exceeds size or file limit")
                data = archive.extractfile(member).read()
                if b"\0" in data:
                    raise InvalidSubmissionError(f"binary source file: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        if not (temporary / "run.sh").is_file():
            raise InvalidSubmissionError("snapshot is missing fixed starter/run.sh")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


class DockerSandbox:
    def __init__(self, image: str, workdir: Path):
        self.image = image
        self.workdir = Path(workdir)
        self.container = None
        self.frozen = False
        self.identity = None

    def __enter__(self):
        self.identity = image_identity(self.image)
        try:
            self.container = _docker("create", *_limits(), self.identity["id"], "-c", "exec sleep infinity")
            _upload_tree(self.container, self.workdir)
            _docker("start", self.container)
            return self
        except BaseException:
            self.close()
            raise

    def execute(self, command: str, timeout_seconds: float = 60) -> dict:
        if self.frozen:
            raise SandboxFrozenError("agent execution is unavailable after freeze")
        if not self.container:
            raise SandboxError("agent execution is unavailable before start/after close")
        if not isinstance(command, str) or not command or timeout_seconds <= 0:
            raise ValueError("command must be nonempty and timeout positive")
        result = _capture(["docker", "exec", self.container, "timeout", "--signal=KILL",
                           str(timeout_seconds), "/bin/sh", "-lc", command], timeout_seconds + 3)
        if result["exit_code"] == 124 or (result["exit_code"] == 137 and result["elapsed_seconds"] >= timeout_seconds):
            result["timed_out"] = True
        if result["timed_out"] or result["output_limited"]:
            # Killing docker exec alone does not kill the process inside Docker.
            self.freeze()
        return result

    def freeze(self):
        if self.container and not self.frozen:
            _docker("stop", "--time", "0", self.container)
            self.frozen = True

    def snapshot(self, destination: Path) -> Path:
        self.freeze()
        if not self.container:
            raise SandboxError("cannot snapshot a closed sandbox")
        # docker cp streams a tar of a stopped filesystem. Limit it before parsing;
        # generated build trees may be large and are deliberately not exported.
        with tempfile.TemporaryDirectory(prefix="prism-snapshot-") as temporary:
            archive = Path(temporary) / "source.tar"
            started = time.monotonic()
            process = subprocess.Popen(["docker", "cp", f"{self.container}:/work/starter", "-"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            size = 0
            try:
                with archive.open("wb") as output:
                    os.set_blocking(process.stdout.fileno(), False)
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    try:
                        done = False
                        while not done:
                            if time.monotonic() - started > 60:
                                raise SandboxError("snapshot export timed out")
                            for key, _ in selector.select(0.1):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    done = True
                                    break
                                size += len(chunk)
                                if size > ARCHIVE_LIMIT:
                                    raise InvalidSubmissionError("snapshot archive exceeds size limit")
                                output.write(chunk)
                    finally:
                        selector.close()
                _, stderr = process.communicate(timeout=10)
                if process.returncode:
                    raise SandboxError(f"snapshot export failed: {stderr.decode('utf-8', 'replace')[:4096]}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()
            return extract_source_archive(archive, Path(destination))

    def close(self):
        if self.container:
            container, self.container = self.container, None
            _docker("rm", "--force", container)

    def __exit__(self, *exc):
        self.close()


class DockerEvaluation:
    """Build once in isolation, then execute each JSON request in a fresh container."""
    def __init__(self, image: str, source: Path, build: str | None = None):
        if build not in (None, "build.sh"):
            raise ValueError("only the fixed starter/build.sh build entrypoint is permitted")
        self.image, self.source, self.build = image, Path(source), build
        self.prepared_image = None
        self.build_result = None

    def __enter__(self):
        with tempfile.TemporaryDirectory(prefix="prism-grade-") as temporary:
            bundle = Path(temporary) / "bundle"
            bundle.mkdir()
            # Host snapshot was already validated. Validate again before upload.
            for name, path in _regular_tree(self.source):
                target = bundle / "starter" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
            with DockerSandbox(self.image, bundle) as sandbox:
                if self.build:
                    self.build_result = sandbox.execute("./starter/build.sh", timeout_seconds=180)
                    if (self.build_result["exit_code"] or self.build_result["timed_out"]
                            or self.build_result["output_limited"]):
                        raise SubmissionBuildError(self.build_result)
                sandbox.freeze()
                self.prepared_image = _docker("commit", sandbox.container)
        return self

    def run_input(self, request: bytes, timeout_seconds: float = 10) -> dict:
        if not self.prepared_image:
            raise SandboxError("evaluation context is not active")
        if not isinstance(request, bytes) or len(request) > OUTPUT_LIMIT or timeout_seconds <= 0:
            raise ValueError("request must be bytes <=1 MiB and timeout positive")
        name = "prism-case-" + uuid.uuid4().hex
        try:
            return _capture(["docker", "run", "--rm", "--name", name, "-i", *_limits(),
                             self.prepared_image, "-c", "exec ./starter/run.sh"],
                            timeout_seconds, stdin=request)
        finally:
            # --rm handles normal completion; force-removal kills timed-out descendants.
            _capture(["docker", "rm", "--force", name], timeout_seconds=15)

    def __exit__(self, *exc):
        if self.prepared_image:
            image, self.prepared_image = self.prepared_image, None
            _docker("image", "rm", image)
