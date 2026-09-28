"""What a single test run showed, reduced to the facts the verdict needs.

Parsers produce ``RunObservation``; ``verdict.decide`` consumes it. Nothing in
here runs a process, so the verdict logic can be tested on recorded data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# The named test's result, as observed in the runner's own per-test output.
NAMED_RESULTS = (
    "passed",
    "failed",
    "skipped",
    "error",  # e.g. a pytest fixture/setup error: the test body never ran
    "timeout",  # the runner itself terminated the test for running too long
    "not_observed",  # no per-test result for the named test at all
    "ambiguous",  # the name matched more than one result, or the output was ambiguous
)


@dataclass
class TestCase:
    __test__ = False  # a data record, not a pytest test class

    name: str  # the identity the user writes in ``test:``
    result: str  # passed | failed | skipped | error | timeout
    group: str = ""  # pytest classname / Rust test binary
    text: str = ""  # the test's own failure output (never other tests')
    message: str | None = None  # the runner's one-line failure message, when it has one


@dataclass
class RunObservation:
    """Facts parsed from one runner invocation."""

    parser: str
    exit: int | None
    timed_out: bool = False
    seconds: float = 0.0
    command: list[str] = field(default_factory=list)
    # Parse outcome. A parse_error means the tool could not read its own
    # evidence: that is UNKNOWN, never a verdict about the test.
    parse_error: str | None = None
    collection_error: str | None = None
    # The harness output could not be attributed with certainty (cargo-text
    # only: interleaved or unreconciled blocks). No test has a result then.
    ambiguous: str | None = None
    cases: list[TestCase] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    log: str | None = None
    output_tail: str = ""

    @property
    def discovered(self) -> int | None:
        """Tests the runner reported actually RUNNING.

        Skipped/ignored tests are not counted (they are in ``skipped``). The
        runners disagree about them: libtest prints an ``#[ignore]``d test as
        "ignored", nextest's JUnit omits it entirely. Counting only tests that
        ran is the one definition both can honour, so the same break gets the
        same verdict under either parser.
        """
        if self.parse_error or self.timed_out:
            return None
        return sum(1 for c in self.cases if c.result != "skipped")

    def count(self, result: str) -> int:
        return sum(1 for c in self.cases if c.result == result)

    def named(self, test: str) -> tuple[str, TestCase | None, int]:
        """(result, case, number of matches) for the named test."""
        if self.ambiguous:
            return "ambiguous", None, 0
        matches = [c for c in self.cases if c.name == test]
        if not matches:
            return "not_observed", None, 0
        if len(matches) > 1:
            return "ambiguous", None, len(matches)
        return matches[0].result, matches[0], 1

    def to_json(self, test: str) -> dict:
        result, case, n = self.named(test)
        return {
            "command": self.command,
            "exit": self.exit,
            "timed_out": self.timed_out,
            "seconds": self.seconds,
            "parser": self.parser,
            "parse_error": self.parse_error,
            "collection_error": self.collection_error,
            "ambiguous_harness_output": self.ambiguous,
            "discovered": self.discovered,
            "passed": self.count("passed"),
            "failed": self.count("failed"),
            "skipped": self.count("skipped"),
            "errors": self.count("error"),
            "named_test": result,
            "named_test_matches": n,
            "notes": list(self.notes),
            "log": self.log,
            "output_tail": self.output_tail,
        }


def case_dict(case: TestCase) -> dict:
    return asdict(case)
