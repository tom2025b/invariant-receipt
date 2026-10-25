"""Milestone 3: non-mutating authoring validation."""

from conftest import run_tool


def test_validate_checks_applicability_runner_and_named_test(py_repo):
    before = run_tool(
        [
            "validate",
            "invariants.yaml",
            "--only",
            "dup-ids-two-way",
            "--only",
            "patch-form",
            "--workdir",
            str(py_repo.parent),
        ],
        py_repo,
    )

    assert before.returncode == 0, before.stderr
    assert "VALID dup-ids-two-way — pytest" in before.stdout
    assert "VALID patch-form — pytest" in before.stdout
    assert not (py_repo / "receipts").exists()


def test_validate_reports_a_stale_find_without_mutating_source(py_repo):
    source = py_repo / "src" / "irfix" / "ids.py"
    original = source.read_bytes()
    spec = py_repo / "invariants.yaml"
    text = spec.read_text()
    spec.write_text(text.replace('find: "if new_id in self._seen:"', 'find: "stale needle"'))

    proc = run_tool(
        [
            "validate",
            "invariants.yaml",
            "--only",
            "dup-ids-two-way",
            "--workdir",
            str(py_repo.parent),
        ],
        py_repo,
    )

    assert proc.returncode == 4
    assert "INVALID dup-ids-two-way" in proc.stdout
    assert "'find' matched 0 times" in proc.stdout
    assert source.read_bytes() == original


def test_validate_rejects_a_missing_only_name(py_repo):
    proc = run_tool(["validate", "invariants.yaml", "--only", "not-there"], py_repo)

    assert proc.returncode == 4
    assert "--only names no invariant: not-there" in proc.stderr
