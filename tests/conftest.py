"""Shared helpers for invariant-receipt's own tests.

End-to-end tests copy a fixture project (fixtures/python-pkg or
fixtures/rust-crate) into a brand-new git repository under tmp_path and run
the real tool against it as a subprocess, importing the package from THIS
checkout's src/ — so a mutation proof that edits src/ in a clone is tested by
the clone's own code, never by an installed copy.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures"
DATA = Path(__file__).resolve().parent / "data"

GIT_ID = ["-c", "user.name=fixture", "-c", "user.email=fixture@invalid"]


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


def make_repo(tmp_path: Path, fixture: str) -> Path:
    """A fresh git repository holding a copy of ``fixtures/<fixture>``, committed."""
    repo = tmp_path / f"{fixture}-repo"
    shutil.copytree(
        FIXTURES / fixture,
        repo,
        ignore=shutil.ignore_patterns("target", "__pycache__", "receipts*"),
    )
    (repo / ".gitignore").write_text("target/\n__pycache__/\nreceipts*/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def cargo_workdir(tmp_path: Path) -> Path:
    """Where the tool's disposable copies (and their cargo target dirs) go.

    Under the lane's CARGO_TARGET_DIR when one is set, so Rust build output
    lands on the build disk; otherwise under tmp_path.
    """
    base = os.environ.get("CARGO_TARGET_DIR")
    if base:
        d = Path(base) / "ir-e2e-work" / tmp_path.name
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = tmp_path / "work"
    d.mkdir()
    return d


def run_tool(
    args: list[str], cwd: Path, env: dict | None = None, timeout: float = 900
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env["PYTHONPATH"] = str(SRC)
    full_env.pop("IR_FIXTURE_TOUCH", None)
    full_env.pop("IR_FIXTURE_FLAKY_DIR", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "invariant_receipt", *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def receipts(out: Path) -> dict[tuple[str, str], dict]:
    found = {}
    for p in sorted(out.glob("*/*/receipt.json")):
        r = json.loads(p.read_text())
        found[(r["invariant"], r["mutation"]["id"])] = r
    return found


def _strict(schema):
    """The shipped schema with additionalProperties:false on every object.

    The shipped schema allows extra fields (spec §5: optional fields may be
    added within a version). Our own receipts are also checked strictly, so a
    misspelt field in the tool's output fails a test instead of passing
    silently.
    """
    s = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if (
                node.get("type") == "object"
                and "properties" in node
                and "additionalProperties" not in node
            ):
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def validate_strict(receipt: dict) -> None:
    from invariant_receipt.receipt import receipt_schema

    jsonschema.Draft202012Validator(receipt_schema()).validate(receipt)
    # Strict only at top level and fixed blocks: allOf/oneOf members describe
    # partial views of the same object and cannot each forbid the others' keys.
    strict = _strict({k: v for k, v in receipt_schema().items() if k not in ("allOf",)})
    run_def = strict["properties"]["mutation"]["properties"]["run"]
    strict["properties"]["mutation"]["properties"]["run"] = {}
    jsonschema.Draft202012Validator(strict).validate(receipt)
    if receipt["mutation"]["run"] is not None:
        merged = copy.deepcopy(strict["$defs"]["run"])
        extra = run_def["oneOf"][1]["allOf"][1]
        merged["properties"].update(extra["properties"])
        merged["required"] = sorted(set(merged["required"]) | set(extra["required"]))
        jsonschema.Draft202012Validator({"$defs": strict["$defs"], **merged}).validate(
            receipt["mutation"]["run"]
        )


@pytest.fixture
def py_repo(tmp_path):
    return make_repo(tmp_path, "python-pkg")


@pytest.fixture
def rs_repo(tmp_path):
    if shutil.which("cargo") is None:
        pytest.fail("cargo is not installed; the Rust fixture cases cannot run")
    return make_repo(tmp_path, "rust-crate")
