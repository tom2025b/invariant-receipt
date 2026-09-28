"""The disposable copy, and proof the original was untouched (spec §7).

All runs happen in a local clone of the committed HEAD. The user's working
tree is only ever READ: ``git status`` (with ``--no-optional-locks`` so git
does not even refresh the index stat cache) and a hash of every tracked file.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SandboxError(RuntimeError):
    pass


def git(args: list[str], cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
    )


def repo_root(path: Path) -> Path:
    proc = git(["rev-parse", "--show-toplevel"], path if path.is_dir() else path.parent)
    if proc.returncode != 0:
        raise SandboxError(f"{path} is not inside a git repository")
    return Path(proc.stdout.decode().strip()).resolve()


def head_commit(root: Path) -> str:
    proc = git(["rev-parse", "--verify", "HEAD^{commit}"], root)
    if proc.returncode != 0:
        raise SandboxError(f"{root} has no commit at HEAD")
    return proc.stdout.decode().strip()


def _excluded(rel: str, excludes: list[str]) -> bool:
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excludes)


def status_lines(root: Path, excludes: list[str]) -> list[str]:
    """``git status --porcelain`` for the whole working tree, untracked files included.

    ``excludes`` are repo-relative paths (the tool's own output directory,
    when it lives inside the repo) — the only writes the tool itself makes.
    """
    proc = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if proc.returncode != 0:
        raise SandboxError(f"git status failed: {proc.stderr.decode(errors='replace').strip()}")
    entries = [e for e in proc.stdout.decode("utf-8", errors="replace").split("\0") if e]
    out = []
    skip_next = False
    for entry in entries:
        if skip_next:  # the original path of a rename
            skip_next = False
            continue
        code, rel = entry[:2], entry[3:]
        if code[0] in "RC":
            skip_next = True
        if _excluded(rel, excludes):
            continue
        out.append(f"{code} {rel}")
    return sorted(out)


def tree_hash(root: Path, excludes: list[str]) -> str:
    """sha256 over every tracked path and the bytes currently on disk for it."""
    proc = git(["ls-files", "-z"], root)
    if proc.returncode != 0:
        raise SandboxError("git ls-files failed")
    h = hashlib.sha256()
    for rel in sorted(
        p for p in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0") if p
    ):
        if _excluded(rel, excludes):
            continue
        path = root / rel
        h.update(rel.encode("utf-8", errors="surrogateescape") + b"\0")
        try:
            if path.is_symlink():
                h.update(b"L" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
            elif path.is_file():
                fh = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        fh.update(chunk)
                mode = b"x" if os.access(path, os.X_OK) else b"-"
                h.update(b"F" + mode + fh.digest())
            else:
                h.update(b"<missing>")
        except OSError as exc:
            h.update(f"<unreadable {exc.errno}>".encode())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


@dataclass
class TreeState:
    status: list[str]
    hash: str

    @property
    def clean(self) -> bool:
        return not self.status


def tree_state(root: Path, excludes: list[str]) -> TreeState:
    return TreeState(status=status_lines(root, excludes), hash=tree_hash(root, excludes))


def clone(root: Path, commit: str, dest: Path) -> None:
    """A local clone of exactly ``commit`` at ``dest``. Raises SandboxError."""
    proc = git(
        ["clone", "--no-hardlinks", "--no-checkout", "--quiet", str(root), str(dest)],
        dest.parent,
        timeout=600,
    )
    if proc.returncode != 0:
        raise SandboxError(f"git clone failed: {proc.stderr.decode(errors='replace').strip()}")
    proc = git(
        ["-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit],
        dest,
        timeout=600,
    )
    if proc.returncode != 0:
        raise SandboxError(
            f"git checkout {commit} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    got = head_commit(dest)
    if got != commit:
        raise SandboxError(f"clone is at {got}, expected {commit}")


def remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def stage_all(clone_root: Path) -> None:
    """Stage every change in the clone, so files a patch CREATED are part of the diff."""
    proc = git(["add", "--all"], clone_root)
    if proc.returncode != 0:
        raise SandboxError(f"git add failed: {proc.stderr.decode(errors='replace').strip()}")


def diff(clone_root: Path) -> str:
    """The staged change against HEAD — the exact edit the mutation made."""
    proc = git(["diff", "--cached", "--no-color", "--no-ext-diff", "--binary", "HEAD"], clone_root)
    if proc.returncode != 0:
        raise SandboxError(f"git diff failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def changed_files(clone_root: Path) -> list[str]:
    proc = git(["diff", "--cached", "--name-only", "-z", "HEAD"], clone_root)
    if proc.returncode != 0:
        raise SandboxError("git diff --name-only failed")
    return [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]
