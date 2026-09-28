"""Build identity and the installer's dirty-tree refusal (spec §8)."""

import shutil
import subprocess
import tomllib

from conftest import GIT_ID, ROOT

from invariant_receipt import __version__
from invariant_receipt.identity import describe


def test_package_version_matches_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["version"] == __version__


def test_installed_identity_is_described_with_its_build_time():
    ident = {
        "name": "invariant-receipt",
        "version": "0.1.0",
        "commit": "abc",
        "build_time": "2026-09-28T16:00:00-04:00",
        "build": "installed",
        "dirty": False,
    }
    assert (
        describe(ident) == "invariant-receipt 0.1.0 (commit abc, built 2026-09-28T16:00:00-04:00)"
    )


def test_source_identity_says_so_and_flags_dirt():
    ident = {
        "name": "invariant-receipt",
        "version": "0.1.0",
        "commit": "abc",
        "build_time": None,
        "build": "source",
        "dirty": True,
    }
    assert (
        describe(ident)
        == "invariant-receipt 0.1.0 (commit abc, source checkout, uncommitted changes)"
    )


def test_install_refuses_a_dirty_tree(tmp_path):
    repo = tmp_path / "ir"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(
            ".git", ".venv*", "__pycache__", "receipts", "target", "*.egg-info"
        ),
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *GIT_ID, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *GIT_ID, "commit", "-qm", "snapshot"], cwd=repo, check=True)
    (repo / "src" / "invariant_receipt" / "verdict.py").write_text("# tampered\n")
    venv = tmp_path / "venv"
    proc = subprocess.run(
        ["bash", "scripts/install.sh", str(venv)], cwd=repo, capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert "refused" in proc.stderr
    assert not venv.exists()  # nothing was installed
