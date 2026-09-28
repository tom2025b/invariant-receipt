"""Parsers against RECORDED real output (spec §6) — cargo test text, nextest
JUnit and pytest JUnit, captured from the fixture projects on 2026-09-28."""

import pytest
from conftest import DATA

from invariant_receipt.observation import RunObservation
from invariant_receipt.runners import cargo_text, junit

NAMED = "tests::rejects_duplicate_id"
PY_NAMED = "tests/test_ids.py::test_rejects_duplicate_id"


def cargo(name: str, exit: int) -> RunObservation:
    obs = RunObservation(parser="cargo-text", exit=exit)
    cargo_text.parse((DATA / "cargo_text" / name).read_text(), obs)
    return obs


# ---------------------------------------------------------------- cargo text


def test_cargo_zero_passed_looks_like_success_and_is_zero_tests():
    obs = cargo("zero_passed.txt", 0)
    assert obs.discovered == 0
    assert obs.named("tests::does_not_exist")[0] == "not_observed"
    assert obs.parse_error is None


def test_cargo_single_pass():
    obs = cargo("pass_single.txt", 0)
    assert (obs.discovered, obs.named(NAMED)[0]) == (1, "passed")


def test_cargo_single_failure_keeps_the_tests_own_panic_text():
    obs = cargo("fail_single.txt", 101)
    result, case, n = obs.named(NAMED)
    assert (result, n, obs.discovered) == ("failed", 1, 1)
    assert "second registration must be rejected" in case.text
    assert "panicked at src/lib.rs:88:14" in case.text


def test_cargo_filtered_out_counts():
    obs = cargo("filtered_out.txt", 0)
    assert obs.discovered == 1
    assert obs.named("tests::clamps_high_values")[0] == "passed"


def test_cargo_should_panic_is_a_pass():
    obs = cargo("should_panic_ok.txt", 0)
    assert obs.named("tests::duplicate_panics_when_unwrapped")[0] == "passed"


def test_cargo_full_lib_run():
    obs = cargo("full_lib.txt", 0)
    assert obs.discovered == 5
    assert obs.count("passed") == 5


def test_cargo_full_run_with_doctests_block():
    obs = cargo("fail_full_with_doctests.txt", 101)
    assert obs.named(NAMED)[0] == "failed"
    assert obs.named("tests::register_accepts_new_id")[0] == "passed"


def test_cargo_compile_error_output_is_a_collection_error_not_a_result():
    obs = cargo("compile_error.txt", 101)
    assert obs.discovered == 0
    assert obs.collection_error


def test_cargo_summary_that_disagrees_with_test_lines_is_ambiguous():
    text = (DATA / "cargo_text" / "pass_single.txt").read_text().replace("1 passed", "2 passed")
    obs = RunObservation(parser="cargo-text", exit=0)
    cargo_text.parse(text, obs)
    assert obs.ambiguous and "summary says 2 passed" in obs.ambiguous
    assert obs.named(NAMED)[0] == "ambiguous"


def test_cargo_binary_that_crashes_mid_run_is_recorded_not_guessed():
    text = (
        "     Running unittests src/lib.rs (target/debug/deps/x)\n\nrunning 3 tests\n"
        "test tests::a ... ok\n"
        "error: test failed, to rerun pass `--lib`\n\nCaused by:\n  process didn't exit successfully (signal: 6, SIGABRT)\n"
    )
    obs = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse(text, obs)
    assert obs.named("tests::a")[0] == "ambiguous"  # even the test that printed "ok"
    assert "ended after 1 result line" in obs.ambiguous
    assert [(c.name, c.result) for c in obs.cases] == [("tests::a", "passed")]  # kept as evidence


def test_cargo_same_name_in_two_binaries_is_ambiguous():
    block = "\nrunning 1 test\ntest tests::x ... ok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n"
    text = (
        "     Running unittests src/lib.rs (a)\n"
        + block
        + "     Running tests/shadow.rs (b)\n"
        + block
    )
    obs = RunObservation(parser="cargo-text", exit=0)
    cargo_text.parse(text, obs)
    assert obs.named("tests::x")[0] == "ambiguous"


# ----------------------------------------------------------------- nextest


def nextest(name: str, exit: int) -> RunObservation:
    obs = RunObservation(parser="nextest-junit", exit=exit)
    junit.parse_nextest(DATA / "nextest" / name, obs)
    return obs


def test_nextest_zero_tests():
    assert nextest("zero.xml", 0).discovered == 0


def test_nextest_failure_carries_the_panic():
    result, case, _ = nextest("fail_single.xml", 100).named(NAMED)
    assert result == "failed"
    assert "second registration must be rejected" in case.text


def test_nextest_full_pass():
    obs = nextest("pass_full.xml", 0)
    assert (obs.discovered, obs.count("passed")) == (5, 5)


def test_nextest_flaky_run_fails_only_the_flaky_test():
    obs = nextest("flaky_second.xml", 100)
    assert obs.named("tests::flaky_on_alternate_runs")[0] == "failed"
    assert obs.named(NAMED)[0] == "passed"


def test_nextest_retry_elements_are_refused(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text(
        '<testsuites><testsuite name="x"><testcase name="t" classname="x">'
        "<flakyFailure message='m'/></testcase></testsuite></testsuites>"
    )
    with pytest.raises(junit.JUnitError):
        junit.parse_nextest(p, RunObservation(parser="nextest-junit", exit=0))


def test_nextest_timeout_is_not_a_failure(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text(
        '<testsuites><testsuite name="x"><testcase name="t" classname="x">'
        '<failure type="test timeout" message="timed out"/></testcase></testsuite></testsuites>'
    )
    obs = RunObservation(parser="nextest-junit", exit=100)
    junit.parse_nextest(p, obs)
    assert obs.named("t")[0] == "timeout"


# ------------------------------------------------------------------ pytest


def pyj(name: str, exit: int, nodeid: str = PY_NAMED) -> RunObservation:
    obs = RunObservation(parser="junit-xml", exit=exit)
    junit.parse_pytest(DATA / "pytest" / name, obs, nodeid)
    return obs


def test_pytest_nodeid_mangling_matches_pytest():
    assert junit.pytest_nodeid_to_case("tests/test_ids.py::test_x") == ("tests.test_ids", "test_x")
    assert junit.pytest_nodeid_to_case("a/b.py::C::t[1-x/y]") == ("a.b.C", "t[1-x/y]")


def test_pytest_pass():
    assert pyj("pass.xml", 0).named(PY_NAMED)[0] == "passed"


def test_pytest_failure_keeps_message_and_location():
    result, case, _ = pyj("fail_did_not_raise.xml", 1).named(PY_NAMED)
    assert result == "failed"
    assert "DID NOT RAISE" in case.message
    assert "tests/test_ids.py:" in case.text


def test_pytest_not_found_is_zero_tests():
    obs = pyj("not_found.xml", 4, "tests/test_ids.py::test_does_not_exist")
    assert obs.discovered == 0


def test_pytest_collection_error_is_flagged_not_counted():
    obs = pyj("collection_error.xml", 4)
    assert obs.collection_error and "irfix_no_such_module" in obs.collection_error
    assert obs.discovered == 0


def test_pytest_parametrised_name_is_not_the_bare_function():
    obs = pyj("parametrised.xml", 0, "tests/test_ids.py::test_clamps")
    assert obs.discovered == 2
    assert obs.named("tests/test_ids.py::test_clamps")[0] == "not_observed"


def test_pytest_fixture_error_is_error_not_failed():
    assert pyj("fixture_error.xml", 1).named(PY_NAMED)[0] == "error"


def test_unreadable_junit_raises(tmp_path):
    p = tmp_path / "bad.xml"
    p.write_text("<testsuites><testsuite")
    with pytest.raises(junit.JUnitError):
        junit.parse_pytest(p, RunObservation(parser="junit-xml", exit=1), PY_NAMED)
