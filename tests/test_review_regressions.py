"""Regression tests for the Codex cross-family review of PR #1 at ba3a109
(~/projects/git-vista-reports/R-invariant-receipt-pr1-codex-2026-09-28.md).
Each reproduces the reviewer's case and was red before its fix."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import DATA, GIT_ID, make_repo, receipts, run_tool

from invariant_receipt import cli, signature
from invariant_receipt.observation import RunObservation, TestCase
from invariant_receipt.runners import cargo_text
from invariant_receipt.verdict import (
    CompileStep,
    Evidence,
    MutationFacts,
    baseline_problem,
    decide,
)

# -- 1 (P1): captured output is never harness evidence ------------------------


def test_nested_harness_log_in_captured_output_invents_no_result():
    """A REAL test binary: `t_sibling` prints a nested run's log ("running 1
    test / test tests::t ... FAILED / test result") and fails. There is no
    test named tests::t, so it must not be observed."""
    obs = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse((DATA / "cargo_text" / "captured_nested_harness.txt").read_text(), obs)
    # Only the harness's own line is kept as evidence; the nested "tests::t"
    # never becomes a case. Since repair cycle 2 a nested block anywhere before
    # the summary also makes the whole run ambiguous: no result for any test.
    assert [(c.name, c.result) for c in obs.cases] == [("tests::t_sibling", "failed")]
    assert obs.named("tests::t")[0] not in ("passed", "failed")
    assert obs.ambiguous


def test_nested_log_cannot_earn_caught_for_a_missing_test():
    obs = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse((DATA / "cargo_text" / "captured_nested_harness.txt").read_text(), obs)
    green = RunObservation(parser="cargo-text", exit=0)
    green.cases = [TestCase("tests::t", "passed")]
    ev = Evidence(
        test="tests::t",
        matches=None,
        baseline_compile=CompileStep("ok"),
        baseline_runs=[green],
        mutation=MutationFacts(True, True, "", CompileStep("ok"), obs),
    )
    assert decide(ev).verdict != "CAUGHT"


# -- 2 (P1): a baseline that is not FULLY green poisons every mutation --------


def _baseline(exit_code, *cases):
    run = RunObservation(parser="cargo-text", exit=exit_code)
    run.cases = list(cases)
    return run


def test_baseline_with_a_failed_sibling_is_not_stable():
    base = _baseline(101, TestCase("t", "passed"), TestCase("t_sibling", "failed"))
    d = baseline_problem(
        Evidence(test="t", matches=None, baseline_compile=CompileStep("ok"), baseline_runs=[base])
    )
    assert d is not None and (d.verdict, d.reason) == ("INVALID", "baseline_not_stable")
    assert "t_sibling" in d.detail


def test_baseline_with_a_nonzero_exit_is_not_stable_even_if_every_parsed_test_passed():
    base = _baseline(101, TestCase("t", "passed"))
    d = baseline_problem(
        Evidence(test="t", matches=None, baseline_compile=CompileStep("ok"), baseline_runs=[base])
    )
    assert d is not None and d.reason == "baseline_not_stable"


def test_red_sibling_baseline_cannot_authorise_caught():
    base = _baseline(101, TestCase("t", "passed"), TestCase("t_sibling", "failed"))
    mrun = _baseline(101, TestCase("t", "failed", text="panicked at src/lib.rs:1:1:\nx"))
    ev = Evidence(
        test="t",
        matches=None,
        baseline_compile=CompileStep("ok"),
        baseline_runs=[base],
        mutation=MutationFacts(True, True, "", CompileStep("ok"), mrun),
    )
    assert (decide(ev).verdict, decide(ev).reason) == ("INVALID", "baseline_not_stable")


def test_skipped_siblings_do_not_make_a_baseline_red():
    base = _baseline(0, TestCase("t", "passed"), TestCase("t_slow", "skipped"))
    assert (
        baseline_problem(
            Evidence(
                test="t", matches=None, baseline_compile=CompileStep("ok"), baseline_runs=[base]
            )
        )
        is None
    )


# -- 3 (P1): diff body lines that look like file headers ------------------------


def test_body_lines_starting_with_dashes_are_kept(tmp_path):
    """Codex's case: a string line '-- old' becomes the deleted diff line
    '--- old'. It is hunk content, not a file header."""
    repo = tmp_path / "r"
    repo.mkdir()
    src = repo / "m.py"
    src.write_text('DOC = """\n-- old\n"""\n\n\ndef test_x():\n    assert False\n')
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *GIT_ID, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *GIT_ID, "commit", "-qm", "x"], cwd=repo, check=True)
    src.write_text('DOC = """\n-- new\n"""\n\n\ndef test_x():\n    assert False\n')
    diff = subprocess.run(
        ["git", "diff"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "\n--- old\n" in diff  # the trap is really in the diff
    # A same-length replacement: the assertion is line 7 before and after.
    t = "m.py:7: AssertionError"
    assert (
        signature.basis("m.py::test_x", t, "AssertionError", diff=diff)[1]
        == "m.py::test_x\n@ m.py:7"
    )


def test_body_lines_starting_with_pluses_are_kept():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -1,3 +1,4 @@\n x = 1\n+++ inserted\n y = 2\n z = 3\n"
    )
    # Original line 3 is line 4 after one inserted line; line 2 is the insertion.
    assert signature.basis("t", "m.py:4: AssertionError", "E", diff=diff)[1] == "t\n@ m.py:3"
    assert signature.basis("t", "m.py:2: AssertionError", "E", diff=diff)[1] == (
        "t\n@ m.py:2 (inside lines the mutation inserted)"
    )


# -- 4 (P1): an argument refusal clears stale evidence ---------------------------


def test_argument_refusal_clears_a_previous_caught_run(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    first = run_tool(
        ["run", "invariants.yaml", "--out", str(out), "--only", "dup-ids-two-way"], repo
    )
    assert first.returncode == 0 and receipts(out)
    proc = run_tool(["run", "invariants.yaml", "--out", str(out), "--repeat", "abc"], repo)
    assert proc.returncode == 4
    assert receipts(out) == {}
    summary = json.loads((out / "summary.json").read_text())
    assert summary["exit_code"] == 4 and summary["refused"]["reason"] == "bad_arguments"
    assert "REFUSED" in (out / "pr-summary.txt").read_text()


def test_argument_refusal_with_equals_form_and_default_out(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out2"
    run_tool(["run", "invariants.yaml", f"--out={out}", "--only", "dup-ids-two-way"], repo)
    assert receipts(out)
    proc = run_tool(["run", "invariants.yaml", f"--out={out}", "--runner", "maven"], repo)
    assert proc.returncode == 4 and receipts(out) == {}


# -- 5 (P2): a render failure leaves durable UNKNOWN evidence, no traceback --------


def test_render_failure_after_finalize_downgrades_to_unknown(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"

    def broken_render(*a, **k):
        raise RuntimeError("simulated render bug")

    monkeypatch.setattr(cli, "render", broken_render)
    code = cli.main(
        ["run", str(repo / "invariants.yaml"), "--out", str(out), "--only", "dup-ids-two-way"]
    )
    err = capsys.readouterr().err
    assert code == 3
    assert "Traceback" not in err and "simulated render bug" in err
    rs = receipts(out)
    assert len(rs) == 2
    for r in rs.values():
        assert (r["verdict"], r["reason"]) == ("UNKNOWN", "internal_error")
        assert "CAUGHT" in r["reason_detail"]  # the pre-render verdict is still on record
    summary = json.loads((out / "summary.json").read_text())
    assert summary["exit_code"] == 3


def test_render_command_failure_is_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    out = tmp_path / "o"
    out.mkdir()

    def broken_render(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "render", broken_render)
    assert cli.main(["render", str(out)]) == 3
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("args", [["bogus-command"], []])
def test_non_run_usage_errors_touch_no_output(tmp_path, args):
    proc = run_tool(args, tmp_path)
    assert proc.returncode == 4
    assert not (tmp_path / "receipts").exists()


# -- Re-check (R2) P1: a child test binary's output on the inherited stdout ------


def _inherited() -> RunObservation:
    obs = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse((DATA / "cargo_text" / "inherited_child_harness.txt").read_text(), obs)
    return obs


def test_inherited_child_harness_output_is_ambiguous_never_a_result():
    """REAL recording: parent tests::t PASSES; tests::t_sibling runs a child test
    binary with Command::status(), whose own tests::t fails, and its block lands
    inside the parent's. The parser must not attribute either result."""
    obs = _inherited()
    assert obs.named("tests::t")[0] not in ("passed", "failed")
    assert obs.ambiguous and "running" in obs.ambiguous


def test_inherited_child_output_cannot_earn_caught_for_a_passing_test():
    green = RunObservation(parser="cargo-text", exit=0)
    green.cases = [TestCase("tests::t", "passed"), TestCase("tests::t_sibling", "passed")]
    ev = Evidence(
        test="tests::t",
        matches=None,
        baseline_compile=CompileStep("ok"),
        baseline_runs=[green],
        mutation=MutationFacts(True, True, "", CompileStep("ok"), _inherited()),
    )
    d = decide(ev)
    assert (d.verdict, d.reason) == ("INVALID", "named_test_not_observed")
    assert d.detail.startswith("ambiguous_harness_output")


def test_a_block_whose_lines_do_not_reconcile_with_its_summary_is_ambiguous():
    text = (DATA / "cargo_text" / "pass_single.txt").read_text().replace("1 passed", "2 passed")
    obs = RunObservation(parser="cargo-text", exit=0)
    cargo_text.parse(text, obs)
    assert obs.ambiguous
    assert obs.named("tests::rejects_duplicate_id")[0] not in ("passed", "failed")


def test_ambiguous_output_at_baseline_poisons_the_invariant():
    d = baseline_problem(
        Evidence(
            test="tests::t",
            matches=None,
            baseline_compile=CompileStep("ok"),
            baseline_runs=[_inherited()],
        )
    )
    assert d is not None and (d.verdict, d.reason) == ("INVALID", "baseline_not_stable")
    assert "ambiguous_harness_output" in d.detail
