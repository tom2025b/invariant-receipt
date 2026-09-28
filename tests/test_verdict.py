"""The per-mutation decision (spec §3), on hand-built evidence. No subprocesses."""

from pathlib import Path

import pytest
from conftest import SRC

from invariant_receipt.observation import RunObservation, TestCase
from invariant_receipt.verdict import (
    CAUGHT,
    INVALID,
    REASONS,
    SURVIVED,
    UNKNOWN,
    CompileStep,
    Evidence,
    MutationFacts,
    decide,
)

NAMED = "tests::rejects_duplicate_id"


def test_package_is_imported_from_this_checkout():
    """Guards every other test: they must exercise THIS tree's src/, so a
    mutation proof that edits a clone's src/ is tested by the clone's code."""
    import invariant_receipt

    assert Path(invariant_receipt.__file__).resolve().is_relative_to(SRC)


def run(*cases, exit=1, **kw) -> RunObservation:
    obs = RunObservation(parser="cargo-text", exit=exit, **kw)
    obs.cases = list(cases)
    return obs


def green() -> RunObservation:
    return run(TestCase(NAMED, "passed"), exit=0)


def failed(
    text="thread 'x' panicked at src/lib.rs:88:14:\nsecond registration must be rejected: ()",
):
    return TestCase(NAMED, "failed", text=text)


def ev(
    mrun=None,
    *,
    matches=None,
    base=None,
    applied=True,
    changed=True,
    compile_status="ok",
    tool_error=None,
):
    return Evidence(
        test=NAMED,
        matches=matches,
        tool_error=tool_error,
        baseline_compile=CompileStep("ok"),
        baseline_runs=[green()] if base is None else base,
        mutation=MutationFacts(
            applied=applied,
            changed=changed,
            apply_detail="",
            compile=CompileStep(compile_status),
            run=mrun,
        ),
    )


# -- CAUGHT needs the named test observed failing AND the oracle ------------


def test_named_test_failing_as_declared_is_caught():
    d = decide(ev(run(failed()), matches="must be rejected"))
    assert (d.verdict, d.reason, d.oracle_matched) == (CAUGHT, "caught", True)


def test_no_matcher_means_any_failure_of_the_named_test():
    d = decide(ev(run(failed())))
    assert (d.verdict, d.reason) == (CAUGHT, "caught")


def test_failure_that_does_not_match_the_oracle_is_invalid_not_caught():
    """Key rule (a): a wrong-reason failure is never promoted to CAUGHT."""
    d = decide(ev(run(failed()), matches="duplicate id 7 was accepted"))
    assert (d.verdict, d.reason, d.oracle_matched) == (INVALID, "oracle_mismatch", False)


def test_oracle_reads_only_the_named_tests_own_output():
    other = TestCase("tests::other", "failed", text="must be rejected")
    d = decide(
        ev(run(failed(text="panicked: something unrelated"), other), matches="must be rejected")
    )
    assert (d.verdict, d.reason) == (INVALID, "oracle_mismatch")


# -- nothing is inferred from an exit code ----------------------------------


def test_other_tests_failing_does_not_matter_named_passed_is_survived():
    """Key rule (b): a non-zero exit caused by OTHER tests is not a catch."""
    d = decide(ev(run(TestCase(NAMED, "passed"), TestCase("tests::other", "failed"), exit=101)))
    assert (d.verdict, d.reason) == (SURVIVED, "named_test_passed")


def test_nonzero_exit_without_the_named_test_is_not_a_catch():
    d = decide(ev(run(TestCase("tests::other", "failed"), exit=101)))
    assert (d.verdict, d.reason) == (INVALID, "named_test_not_observed")


def test_named_passed_with_clean_exit_is_survived():
    d = decide(ev(run(TestCase(NAMED, "passed"), exit=0)))
    assert (d.verdict, d.reason) == (SURVIVED, "named_test_passed")


def test_parser_contradicting_the_exit_code_is_unknown():
    d = decide(ev(run(failed(), exit=0)))
    assert (d.verdict, d.reason) == (UNKNOWN, "internal_error")


# -- step 3: applied, compiled, collected -----------------------------------


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"applied": False}, "mutation_not_applied"),
        ({"changed": False}, "mutation_no_change"),
        ({"compile_status": "failed"}, "compile_failed"),
        ({"compile_status": "timeout"}, "timeout"),
    ],
)
def test_mutation_step_failures_are_invalid(kw, reason):
    d = decide(ev(run(failed()), **kw))
    assert (d.verdict, d.reason) == (INVALID, reason)


def test_run_timeout_is_invalid_never_caught():
    d = decide(ev(run(exit=None, timed_out=True)))
    assert (d.verdict, d.reason) == (INVALID, "timeout")


def test_runner_terminating_the_named_test_is_timeout_not_caught():
    d = decide(ev(run(TestCase(NAMED, "timeout"), exit=100)))
    assert (d.verdict, d.reason) == (INVALID, "timeout")


def test_collection_failure_is_invalid():
    d = decide(ev(run(exit=2, collection_error="ImportError")))
    assert (d.verdict, d.reason) == (INVALID, "collection_failed")


def test_zero_tests_run_is_invalid_even_with_exit_zero():
    d = decide(ev(run(exit=0)))
    assert (d.verdict, d.reason) == (INVALID, "zero_tests")


def test_only_skipped_tests_counts_as_zero_tests():
    d = decide(ev(run(TestCase(NAMED, "skipped"), exit=0)))
    assert (d.verdict, d.reason) == (INVALID, "zero_tests")


@pytest.mark.parametrize("result", ["error", "skipped"])
def test_named_test_that_did_not_run_its_body_is_not_observed(result):
    d = decide(ev(run(TestCase(NAMED, result), TestCase("t::other", "passed"))))
    assert (d.verdict, d.reason) == (INVALID, "named_test_not_observed")


def test_ambiguous_name_is_not_observed():
    d = decide(ev(run(failed(), TestCase(NAMED, "failed", group="other-binary"))))
    assert (d.verdict, d.reason) == (INVALID, "named_test_not_observed")


def test_unparseable_output_is_unknown():
    d = decide(ev(run(exit=101, parse_error="summary mismatch")))
    assert (d.verdict, d.reason) == (UNKNOWN, "internal_error")


# -- step 2: the baseline poisons every mutation ------------------------------


def test_baseline_red_on_any_repeat_is_not_stable():
    base = [green(), run(failed(), exit=101), green()]
    d = decide(ev(run(failed()), base=base))
    assert (d.verdict, d.reason) == (INVALID, "baseline_not_stable")
    assert "run 2 of 3" in d.detail


def test_baseline_with_zero_tests():
    d = decide(ev(run(failed()), base=[run(exit=0)]))
    assert (d.verdict, d.reason) == (INVALID, "baseline_no_tests")


def test_baseline_that_never_observes_the_named_test():
    d = decide(
        ev(run(failed()), base=[run(TestCase("tests::rejects_duplicate_id_2", "passed"), exit=0)])
    )
    assert (d.verdict, d.reason) == (INVALID, "named_test_not_found")


def test_baseline_compile_failure():
    e = ev(run(failed()))
    e.baseline_compile = CompileStep("failed", "error[E0308]")
    assert decide(e).reason == "compile_failed"


def test_baseline_timeout():
    d = decide(ev(run(failed()), base=[run(exit=None, timed_out=True)]))
    assert (d.verdict, d.reason) == (INVALID, "timeout")


def test_baseline_problem_wins_over_a_perfect_mutation_run():
    d = decide(ev(run(failed()), matches="rejected", base=[green(), run(exit=0)]))
    assert d.verdict == INVALID


# -- step 1: the tool itself ------------------------------------------------


@pytest.mark.parametrize("reason", ["runner_missing", "copy_failed", "internal_error"])
def test_tool_errors_are_unknown_and_come_first(reason):
    d = decide(ev(run(failed()), tool_error=(reason, "x"), base=[]))
    assert (d.verdict, d.reason) == (UNKNOWN, reason)


def test_every_reason_is_in_the_spec_table():
    spec_invalid = {
        "baseline_not_stable",
        "baseline_no_tests",
        "named_test_not_found",
        "mutation_not_applied",
        "mutation_no_change",
        "compile_failed",
        "collection_failed",
        "zero_tests",
        "named_test_not_observed",
        "timeout",
        "oracle_mismatch",
    }
    assert set(REASONS[INVALID]) == spec_invalid
    assert set(REASONS[UNKNOWN]) == {
        "runner_missing",
        "copy_failed",
        "internal_error",
        "source_tree_changed",
    }
