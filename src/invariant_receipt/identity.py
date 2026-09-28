"""Build identity: one answer for --version, every receipt's tool block, and the
HTML footer (spec §8).

An installed copy carries a generated ``_build.py`` written by
``scripts/install.sh`` from a clean tree. A source checkout has no such file, so
its identity is read from git at run time and says plainly that it is a source
checkout, and whether that checkout is dirty.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import __version__

NAME = "invariant-receipt"


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def build_identity() -> dict:
    try:
        from ._build import BUILD  # type: ignore[import-not-found]
    except ImportError:
        BUILD = None
    if BUILD is not None:
        return {
            "name": NAME,
            "version": BUILD["version"],
            "commit": BUILD["commit"],
            "build_time": BUILD["build_time"],
            "build": "installed",
            "dirty": False,
        }
    here = Path(__file__).resolve().parent
    commit = _git(["rev-parse", "HEAD"], here)
    status = _git(["status", "--porcelain", "--", "."], here) if commit else None
    return {
        "name": NAME,
        "version": __version__,
        "commit": commit or "unknown",
        "build_time": None,
        "build": "source",
        "dirty": bool(status) if status is not None else None,
    }


def describe(identity: dict) -> str:
    """The one-line identity string shared by --version and the HTML footer."""
    commit = identity.get("commit") or "unknown"
    if identity.get("build") == "installed":
        where = f"built {identity.get('build_time')}"
    else:
        where = "source checkout"
        if identity.get("dirty"):
            where += ", uncommitted changes"
    return f"{identity.get('name', NAME)} {identity.get('version')} (commit {commit}, {where})"
