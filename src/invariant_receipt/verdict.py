"""The per-mutation verdict (spec §3). Pure: no processes, no files.

The decision runs in a fixed order, so the same evidence always gives the same
answer:

    1. did the tool run?              no  -> UNKNOWN
    2. was the baseline green, every repeat?   no  -> INVALID
    3. was the break applied, compiled, collected?   no  -> INVALID
    4. was the named test observed?   no  -> INVALID
    5. did the named test fail?       no  -> SURVIVED
    6. did the failure match the oracle?   no  -> INVALID
                                      yes -> CAUGHT

Nothing is inferred from an exit code. A run's exit status is recorded, and it
is cross-checked against the per-test results (a contradiction means the tool
misread its own evidence, which is UNKNOWN), but it is never itself the reason
for CAUGHT or SURVIVED. Only the named test's own observed result counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .observation import RunObservation

CAUGHT = "CAUGHT"
SURVIVED = "SURVIVED"
INVALID = "INVALID"
UNKNOWN = "UNKNOWN"
VERDICTS = (CAUGHT, SURVIVED, INVALID, UNKNOWN)

REASONS = {
    CAUGHT: ("caught",),
    SURVIVED: ("named_test_passed",),
    INVALID: (
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
    ),
    UNKNOWN: ("runner_missing", "copy_failed", "internal_error", "source_tree_changed"),
}


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str
    detail: str
    oracle_matched: bool | None = None


@dataclass
class CompileStep:
    status: str  # ok | failed | timeout | skipped
    detail: str = ""


@dataclass
class MutationFacts:
    applied: bool
    changed: bool
    apply_detail: str = ""
    compile: CompileStep | None = None
    run: RunObservation | None = None


@dataclass
class Evidence:
    """Everything the decision reads, gathered by the engine."""

    test: str
    matches: str | None
    tool_error: tuple[str, str] | None = None  # (UNKNOWN reason, detail)
    baseline_compile: CompileStep | None = None
    baseline_runs: list[RunObservation] = field(default_factory=list)
    mutation: MutationFacts | None = None


def oracle_matches(matches: str | None, failure_text: str) -> bool:
    """The declared failure oracle. With no ``matches`` the oracle is the failure itself."""
    if matches is None:
        return True
    return re.search(matches, failure_text, re.MULTILINE) is not None


def _run_unreadable(run: RunObservation) -> str | None:
    """A reason the tool cannot trust its reading of ``run``, or None."""
    if run.parse_error:
        return f"could not parse runner output: {run.parse_error}"
    return None


def baseline_problem(ev: Evidence) -> Decision | None:
    """Step 2. A baseline problem poisons every mutation of the invariant."""
    comp = ev.baseline_compile
    if comp is not None and comp.status == "timeout":
        return Decision(INVALID, "timeout", f"baseline build timed out. {comp.detail}".strip())
    if comp is not None and comp.status == "failed":
        return Decision(
            INVALID, "compile_failed", f"the unmodified code did not build. {comp.detail}".strip()
        )
    if not ev.baseline_runs:
        return Decision(UNKNOWN, "internal_error", "no baseline run was recorded")
    total = len(ev.baseline_runs)
    for i, run in enumerate(ev.baseline_runs, start=1):
        where = f"baseline run {i} of {total}"
        unreadable = _run_unreadable(run)
        if unreadable:
            return Decision(UNKNOWN, "internal_error", f"{where}: {unreadable}")
        if run.timed_out:
            return Decision(INVALID, "timeout", f"{where} timed out after {run.seconds}s")
        if run.collection_error:
            return Decision(INVALID, "collection_failed", f"{where}: {run.collection_error}")
        if run.discovered == 0:
            return Decision(INVALID, "baseline_no_tests", f"{where} discovered zero tests")
        result, _case, n = run.named(ev.test)
        if result == "not_observed":
            return Decision(
                INVALID,
                "named_test_not_found",
                f"{where} ran {run.discovered} test(s) but none was named {ev.test!r}",
            )
        if result == "ambiguous":
            return Decision(
                INVALID,
                "named_test_not_found",
                f"{where}: {n} results are named {ev.test!r}; the name must pick exactly one "
                f"test (set 'target' for Rust)",
            )
        if result != "passed":
            return Decision(
                INVALID,
                "baseline_not_stable",
                f"{where}: the named test was {result} on the unmodified code",
            )
    return None


def decide(ev: Evidence) -> Decision:
    # 1 · did the tool run?
    if ev.tool_error is not None:
        reason, detail = ev.tool_error
        return Decision(UNKNOWN, reason, detail)

    # 2 · baseline green on every repeat?
    problem = baseline_problem(ev)
    if problem is not None:
        return problem

    # 3 · break applied, compiled, collected?
    m = ev.mutation
    if m is None:
        return Decision(UNKNOWN, "internal_error", "the mutation step recorded nothing")
    if not m.applied:
        return Decision(INVALID, "mutation_not_applied", m.apply_detail)
    if not m.changed:
        return Decision(
            INVALID, "mutation_no_change", m.apply_detail or "the edit changed no bytes"
        )
    if m.compile is not None and m.compile.status == "timeout":
        return Decision(
            INVALID, "timeout", f"build of the mutated code timed out. {m.compile.detail}"
        )
    if m.compile is not None and m.compile.status == "failed":
        return Decision(INVALID, "compile_failed", m.compile.detail)
    run = m.run
    if run is None:
        return Decision(UNKNOWN, "internal_error", "the mutated code was never run")
    unreadable = _run_unreadable(run)
    if unreadable:
        return Decision(UNKNOWN, "internal_error", unreadable)
    if run.timed_out:
        return Decision(INVALID, "timeout", f"the mutation run timed out after {run.seconds}s")
    if run.collection_error:
        return Decision(INVALID, "collection_failed", run.collection_error)
    if run.discovered == 0:
        return Decision(INVALID, "zero_tests", "the mutation run discovered zero tests")

    # 4 · named test observed?
    result, case, n = run.named(ev.test)
    if result == "timeout":
        return Decision(
            INVALID, "timeout", "the runner terminated the named test for running too long"
        )
    if result not in ("passed", "failed"):
        detail = {
            "not_observed": f"no result for {ev.test!r} among {run.discovered} test result(s)",
            "ambiguous": f"{n} results are named {ev.test!r}",
            "skipped": "the named test was skipped, so it never ran",
            "error": "the named test errored before its body ran (setup/teardown error)",
        }.get(result, result)
        return Decision(INVALID, "named_test_not_observed", detail)
    assert case is not None

    # Cross-check, never a source of verdicts: a failed named test with a
    # clean exit means the parse and the process disagree.
    if result == "failed" and run.exit == 0:
        return Decision(
            UNKNOWN,
            "internal_error",
            "the parser read the named test as failed, but the runner exited 0",
        )

    # 5 · did the named test fail? Other tests failing does not matter.
    named_failed = result == "failed"
    if not named_failed:
        return Decision(
            SURVIVED, "named_test_passed", "the named test ran and passed on the broken code"
        )

    # 6 · did it fail as declared?
    oracle_matched = oracle_matches(ev.matches, case.text)
    if not oracle_matched:
        return Decision(
            INVALID,
            "oracle_mismatch",
            f"the named test failed, but its output does not match {ev.matches!r}",
            oracle_matched=False,
        )
    return Decision(
        CAUGHT, "caught", "the named test ran and failed as declared", oracle_matched=True
    )
