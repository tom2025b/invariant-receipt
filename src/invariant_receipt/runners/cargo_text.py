"""Parse ``cargo test`` human-readable output (the fallback when nextest is absent).

``cargo test``'s JSON output is nightly-only, so stable Rust leaves text. This
parser is deliberately suspicious: every ``test result:`` summary is checked
against the per-test lines it follows, and a mismatch is a parse error (the
tool's evidence is unreadable -> UNKNOWN), never a guess.

It expects stdout and stderr merged in order, as ``procs.run_logged`` records
them, so "Running <binary>" lines land before that binary's results.

Captured test output is NEVER read as harness evidence. With capture on (the
default; the tool never turns it off) libtest prints, per test binary:

    running N tests
    test <name> ... ok|FAILED|ignored        <- exactly N harness lines
    failures:                                <- only if something failed
    ---- <name> stdout ----                  <- captured output, verbatim
    ...anything a test printed...
    failures:
        <each failed name>                   <- the closing list
    test result: ...

A failing test's captured output can contain text that looks exactly like a
harness (a test that drives a nested ``cargo test`` prints "running 1 test",
"test t ... FAILED" and a "test result:" line). So results are taken ONLY from
the N lines right after "running N tests", and the captured region is skipped
as a whole: it ends at the "failures:" line followed by exactly this block's
failed names and then the summary (Codex review of PR #1, finding 1).
"""

from __future__ import annotations

import re

from ..observation import RunObservation, TestCase

RUNNING_RE = re.compile(r"^\s+(?:Running|Doc-tests)\s+(?P<what>.+?)\s*$")
BLOCK_RE = re.compile(r"^running (?P<n>\d+) tests?$")
TEST_RE = re.compile(
    r"^test (?P<name>.+?)(?P<sp> - should panic)? \.\.\. (?P<res>ok|FAILED|ignored(?:, .*)?)$"
)
RESULT_RE = re.compile(
    r"^test result: (?P<status>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
    r"(?P<ignored>\d+) ignored; (?P<measured>\d+) measured; (?P<filtered>\d+) filtered out"
)
STDOUT_HDR_RE = re.compile(r"^---- (?P<name>.+?) stdout ----$")
SLOW_RE = re.compile(r"^test .+ has been running for over \d+ seconds$")


class CargoTextError(ValueError):
    pass


def _check_summary(binary: str, summary: re.Match, cases: list[TestCase], expected: int) -> None:
    passed = sum(1 for c in cases if c.result == "passed")
    failed = sum(1 for c in cases if c.result == "failed")
    ignored = sum(1 for c in cases if c.result == "skipped")
    want = (int(summary["passed"]), int(summary["failed"]), int(summary["ignored"]))
    if (passed, failed, ignored) != want or len(cases) != expected:
        raise CargoTextError(
            f"{binary}: summary says {want[0]} passed/{want[1]} failed/{want[2]} ignored of "
            f"{expected}, but the harness lines show {passed}/{failed}/{ignored} of {len(cases)}"
        )


def _next_nonblank(lines: list[str], i: int) -> int:
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def _closing_failures(lines: list[str], start: int, failed: list[str]) -> int | None:
    """Index of the harness's closing "failures:" line for this block, or None."""
    want = sorted(failed)
    for k in range(start, len(lines)):
        if lines[k] != "failures:":
            continue
        listed = lines[k + 1 : k + 1 + len(failed)]
        if len(listed) != len(failed) or not all(x.startswith("    ") for x in listed):
            continue
        if sorted(x[4:] for x in listed) != want:
            continue
        j = _next_nonblank(lines, k + 1 + len(failed))
        if j < len(lines) and RESULT_RE.match(lines[j]):
            return k
    return None


def _captured_text(region: list[str], failed: list[str]) -> dict[str, str]:
    """Split the captured region into each failed test's own output."""
    texts: dict[str, list[str]] = {}
    current: str | None = None
    for line in region:
        m = STDOUT_HDR_RE.match(line)
        if m and m["name"] in failed and m["name"] not in texts:
            current = m["name"]
            texts[current] = []
            continue
        if current is not None:
            texts[current].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in texts.items()}


def _block(lines: list[str], i: int, expected: int, binary: str, obs: RunObservation) -> int:
    """Parse one binary's results starting after "running N tests". Returns the next index."""
    cases: list[TestCase] = []
    seen: set[str] = set()
    # Phase 1: the harness's own per-test lines, and nothing else.
    while i < len(lines) and len(cases) < expected:
        line = lines[i]
        t = TEST_RE.match(line)
        if t:
            name = t["name"]
            if name in seen:
                raise CargoTextError(f"{binary}: the harness reported {name!r} twice")
            seen.add(name)
            res = t["res"]
            result = "passed" if res == "ok" else "failed" if res == "FAILED" else "skipped"
            cases.append(TestCase(name=name, result=result, group=binary))
            i += 1
            continue
        if RESULT_RE.match(line) or BLOCK_RE.match(line) or RUNNING_RE.match(line):
            break
        if line.strip() and not SLOW_RE.match(line):
            obs.notes.append(f"{binary}: uncaptured output between result lines: {line[:120]!r}")
        i += 1
    if len(cases) < expected:
        obs.notes.append(
            f"{binary or 'a test binary'}: 'running {expected} tests' but only {len(cases)} "
            f"result line(s) before the output ended or moved on — the binary stopped early"
        )
        obs.cases.extend(cases)
        return i

    failed = [c.name for c in cases if c.result == "failed"]
    if not failed:
        j = _next_nonblank(lines, i)
        m = RESULT_RE.match(lines[j]) if j < len(lines) else None
        if m is None:
            found = lines[j][:80] if j < len(lines) else "end of output"
            raise CargoTextError(
                f"{binary}: expected 'test result:' after {expected} results, found {found!r}"
            )
        _check_summary(binary, m, cases, expected)
        obs.cases.extend(cases)
        return j + 1

    k = _closing_failures(lines, i, failed)
    if k is None:
        raise CargoTextError(
            f"{binary}: the failures section never closed with a 'failures:' list naming "
            f"{failed}; captured output cannot be told apart from the harness's own"
        )
    texts = _captured_text(lines[i:k], failed)
    j = _next_nonblank(lines, k + 1 + len(failed))
    _check_summary(binary, RESULT_RE.match(lines[j]), cases, expected)
    for c in cases:
        c.text = texts.get(c.name, "")
    obs.cases.extend(cases)
    return j + 1


def parse(output: str, obs: RunObservation) -> None:
    """Fill ``obs`` from merged ``cargo test`` output. Raises CargoTextError."""
    lines = output.splitlines()
    binary = ""
    blocks = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        m = RUNNING_RE.match(line)
        if m:
            binary = m["what"]
            i += 1
            continue
        m = BLOCK_RE.match(line)
        if m:
            blocks += 1
            i = _block(lines, i + 1, int(m["n"]), binary, obs)
            continue
        i += 1
    if blocks == 0 and obs.exit not in (0, None):
        obs.collection_error = (
            "cargo test exited non-zero and no test binary reported 'running N tests'"
        )
