"""End to end on the Rust fixture, under BOTH cargo-nextest and `cargo test`
text parsing (spec §9): same verdicts, only receipt.runner.parser differs."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import cargo_workdir, make_repo, receipts, run_tool, validate_strict

pytestmark = [pytest.mark.e2e, pytest.mark.rust]

MAIN = [
    "dup-ids-two-way",
    "dup-ids-one-way",
    "weak-test-gap",
    "invalid-cases",
    "patch-form",
    "filter-selects-nothing",
    "name-matches-no-single-test",
]

EXPECTED = {
    ("dup-ids-two-way", "drop-the-check"): ("CAUGHT", "caught"),
    ("dup-ids-two-way", "store-then-reject"): ("CAUGHT", "caught"),
    ("dup-ids-one-way", "drop-the-check"): ("CAUGHT", "caught"),
    ("dup-ids-one-way", "look-for-the-wrong-id"): ("CAUGHT", "caught"),
    ("weak-test-gap", "drop-the-check"): ("SURVIVED", "named_test_passed"),
    ("invalid-cases", "compile-break"): ("INVALID", "compile_failed"),
    ("invalid-cases", "wrong-oracle"): ("INVALID", "oracle_mismatch"),
    ("invalid-cases", "find-not-present"): ("INVALID", "mutation_not_applied"),
    ("invalid-cases", "changes-nothing"): ("INVALID", "mutation_no_change"),
    ("invalid-cases", "rename-the-test"): ("INVALID", "zero_tests"),
    ("invalid-cases", "ignore-the-test"): ("INVALID", "zero_tests"),
    ("invalid-cases", "second-test-with-the-same-name"): ("INVALID", "named_test_not_observed"),
    ("patch-form", "drop-the-check-as-patch"): ("CAUGHT", "caught"),
    ("filter-selects-nothing", "drop-the-check"): ("INVALID", "baseline_no_tests"),
    ("name-matches-no-single-test", "drop-the-check"): ("INVALID", "named_test_not_found"),
}

STATUSES = {
    "dup-ids-two-way": "TWO-WAY VERIFIED",
    "dup-ids-one-way": "ONE-WAY",
    "weak-test-gap": "GAP FOUND",
    "invalid-cases": "INCOMPLETE",
    "patch-form": "ONE-WAY",
    "filter-selects-nothing": "INCOMPLETE",
    "name-matches-no-single-test": "INCOMPLETE",
}

PARSER = {"nextest": "nextest-junit", "cargo": "cargo-text"}


def only(*names):
    return [a for n in names for a in ("--only", n)]


def nextest_installed() -> bool:
    if shutil.which("cargo") is None:
        return False
    return subprocess.run(["cargo", "nextest", "--version"], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    if not nextest_installed():
        pytest.fail("cargo-nextest is not installed; spec §9 requires both Rust runners")
    tmp = tmp_path_factory.mktemp("rs-main")
    repo = make_repo(tmp, "rust-crate")
    work = cargo_workdir(tmp)
    results = {}
    for runner in ("nextest", "cargo"):
        out = tmp / f"out-{runner}"
        proc = run_tool(
            [
                "run",
                "invariants.yaml",
                "--runner",
                runner,
                "--out",
                str(out),
                "--workdir",
                str(work),
                *only(*MAIN),
            ],
            repo,
        )
        results[runner] = (out, proc)
    return repo, results


@pytest.mark.parametrize("runner", ["nextest", "cargo"])
def test_every_rust_fixture_case_gets_its_stated_verdict(runs, runner):
    _repo, results = runs
    out, proc = results[runner]
    got = {k: (r["verdict"], r["reason"]) for k, r in receipts(out).items()}
    assert got == EXPECTED, proc.stderr[-3000:]
    summary = json.loads((out / "summary.json").read_text())
    assert {i["invariant"]: i["status"] for i in summary["invariants"]} == STATUSES
    assert proc.returncode == 1
    for r in receipts(out).values():
        validate_strict(r)
        assert r["runner"]["kind"] == runner
        assert r["runner"]["parser"] == PARSER[runner]
        if r["mutation"]["run"]:
            assert r["mutation"]["run"]["parser"] == PARSER[runner]
        assert r["source"]["tree_unchanged"] is True


@pytest.mark.parametrize("runner", ["nextest", "cargo"])
def test_nonzero_exit_from_another_test_is_still_survived(runs, runner):
    """Key rule (b), end to end: the exit code is not the named test."""
    _repo, results = runs
    r = receipts(results[runner][0])[("weak-test-gap", "drop-the-check")]
    run = r["mutation"]["run"]
    assert run["exit"] not in (0, None)
    assert run["failed"] == 1 and run["named_test_result"] == "passed"
    assert (r["verdict"], r["reason"]) == ("SURVIVED", "named_test_passed")


def test_both_rust_parsers_agree_on_every_verdict_and_signature(runs):
    _repo, results = runs
    nx, ct = receipts(results["nextest"][0]), receipts(results["cargo"][0])
    assert nx.keys() == ct.keys()
    for k in nx:
        a, b = nx[k], ct[k]
        assert (a["verdict"], a["reason"]) == (b["verdict"], b["reason"]), k
        assert a["mutation"]["fingerprint"] == b["mutation"]["fingerprint"], k
        sa = (a["mutation"]["run"] or {}).get("failure_signature")
        sb = (b["mutation"]["run"] or {}).get("failure_signature")
        assert sa == sb, k
        assert a["runner"]["parser"] != b["runner"]["parser"]


@pytest.mark.parametrize("runner", ["nextest", "cargo"])
def test_rust_flaky_baseline_is_not_stable(tmp_path, runner):
    repo = make_repo(tmp_path, "rust-crate")
    flaky = tmp_path / "flaky"
    flaky.mkdir()
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--runner",
            runner,
            "--repeat",
            "5",
            "--out",
            str(out),
            "--workdir",
            str(cargo_workdir(tmp_path)),
            *only("flaky-baseline"),
        ],
        repo,
        env={"IR_FIXTURE_FLAKY_DIR": str(flaky)},
    )
    (r,) = receipts(out).values()
    assert (r["verdict"], r["reason"]) == ("INVALID", "baseline_not_stable"), proc.stderr[-2000:]
    assert [x["named_test"] for x in r["baseline"]["runs"]] == ["passed", "failed"]


def test_auto_prefers_nextest_and_records_it(tmp_path):
    repo = make_repo(tmp_path, "rust-crate")
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--out",
            str(out),
            "--workdir",
            str(cargo_workdir(tmp_path)),
            *only("patch-form"),
        ],
        repo,
    )
    (r,) = receipts(out).values()
    assert r["runner"]["requested"] == "auto"
    assert (r["runner"]["kind"], r["runner"]["parser"]) == ("nextest", "nextest-junit")
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_project_nextest_config_cannot_add_retries_or_hide_the_junit(tmp_path):
    """A project's own .config/nextest.toml outranks the tool's config. Retries
    are forced off on the command line, and the JUnit is found wherever the
    project's store puts it — but only if it carries this run's id."""
    repo = make_repo(tmp_path, "rust-crate")
    cfg = repo / ".config" / "nextest.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        '[store]\ndir = "elsewhere/store"\n\n[profile.default]\nretries = 3\n\n'
        '[profile."invariant-receipt:run"]\nretries = 2\n'
    )
    subprocess.run(
        ["git", "-c", "user.name=f", "-c", "user.email=f@invalid", "add", "-A"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=f", "-c", "user.email=f@invalid", "commit", "-qm", "cfg"],
        cwd=repo,
        check=True,
    )
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--runner",
            "nextest",
            "--out",
            str(out),
            "--workdir",
            str(cargo_workdir(tmp_path)),
            *only("dup-ids-two-way"),
        ],
        repo,
    )
    rs = receipts(out)
    assert {(r["verdict"], r["reason"]) for r in rs.values()} == {("CAUGHT", "caught")}, (
        proc.stderr[-2000:]
    )
    for r in rs.values():
        run = r["mutation"]["run"]
        assert any("elsewhere/store" in n for n in run["notes"])
        log = (out / run["log"]).read_text()
        assert "TRY 2" not in log and "retry" not in log.lower()
