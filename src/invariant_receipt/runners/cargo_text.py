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


class _Ambiguous(Exception):
    """The harness output cannot be attributed with certainty."""


def _check_summary(binary: str, summary: re.Match, cases: list[TestCase], expected: int) -> None:
    passed = sum(1 for c in cases if c.result == "passed")
    failed = sum(1 for c in cases if c.result == "failed")
    ignored = sum(1 for c in cases if c.result == "skipped")
    want = (int(summary["passed"]), int(summary["failed"]), int(summary["ignored"]))
    if (passed, failed, ignored) != want or len(cases) != expected:
        raise _Ambiguous(
            f"{binary}: its summary says {want[0]} passed/{want[1]} failed/{want[2]} ignored of "
            f"{expected}, but the per-test lines show {passed}/{failed}/{ignored} of {len(cases)}"
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
    """Parse one binary's results starting after "running N tests". Returns the next index.

    Raises _Ambiguous whenever the block cannot be read with certainty. The
    per-test lines read so far are kept in ``obs.cases`` as evidence, but an
    ambiguous run yields no result for ANY test (``RunObservation.named``).
    """
    cases: list[TestCase] = []
    seen: set[str] = set()
    where = binary or "a test binary"
    try:
        # Phase 1: the harness's own per-test lines, and nothing else.
        while len(cases) < expected:
            if i >= len(lines):
                raise _Ambiguous(
                    f"{where}: 'running {expected} tests' but the output ended after "
                    f"{len(cases)} result line(s)"
                )
            line = lines[i]
            t = TEST_RE.match(line)
            if t:
                name = t["name"]
                if name in seen:
                    raise _Ambiguous(f"{where}: a result for {name!r} appeared twice")
                seen.add(name)
                res = t["res"]
                result = "passed" if res == "ok" else "failed" if res == "FAILED" else "skipped"
                cases.append(TestCase(name=name, result=result, group=binary))
                i += 1
                continue
            if BLOCK_RE.match(line):
                # Typically a child test binary run by a test, printing to the
                # inherited stdout: its block lands inside this one.
                raise _Ambiguous(
                    f"{where}: a second 'running N tests' began before this block's summary "
                    f"(after {len(cases)} of {expected} result lines)"
                )
            if RESULT_RE.match(line) or RUNNING_RE.match(line):
                raise _Ambiguous(
                    f"{where}: the block ended or moved on after {len(cases)} of {expected} "
                    f"result lines (the binary stopped early, or output was interleaved)"
                )
            if line.strip() and not SLOW_RE.match(line):
                obs.notes.append(f"{where}: uncaptured output between result lines: {line[:120]!r}")
            i += 1

        failed = [c.name for c in cases if c.result == "failed"]
        if not failed:
            j = _next_nonblank(lines, i)
            m = RESULT_RE.match(lines[j]) if j < len(lines) else None
            if m is None:
                found = lines[j][:80] if j < len(lines) else "end of output"
                raise _Ambiguous(
                    f"{where}: expected 'test result:' after {expected} results, found {found!r}"
                )
            _check_summary(where, m, cases, expected)
            return j + 1

        k = _closing_failures(lines, i, failed)
        if k is None:
            raise _Ambiguous(
                f"{where}: the failures section never closed with a 'failures:' list naming "
                f"exactly {failed}"
            )
        region = lines[i:k]
        for line in region:
            if BLOCK_RE.match(line):
                raise _Ambiguous(
                    f"{where}: a 'running N tests' line appeared inside the failures section, "
                    f"before this block's summary"
                )
            t = TEST_RE.match(line)
            if t and t["name"] in seen:
                raise _Ambiguous(
                    f"{where}: a second result line for {t['name']!r} appeared before this "
                    f"block's summary"
                )
        texts = _captured_text(region, failed)
        j = _next_nonblank(lines, k + 1 + len(failed))
        _check_summary(where, RESULT_RE.match(lines[j]), cases, expected)
        for c in cases:
            c.text = texts.get(c.name, "")
        return j + 1
    finally:
        obs.cases.extend(cases)


def parse(output: str, obs: RunObservation) -> None:
    """Fill ``obs`` from merged ``cargo test`` output.

    cargo-text is the FALLBACK parser (nextest is preferred). It refuses
    ambiguity rather than guessing: any structural surprise sets
    ``obs.ambiguous`` and the run gives no result for any test.
    """
    lines = output.splitlines()
    binary = ""
    blocks = 0
    i = 0
    try:
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
            if TEST_RE.match(line):
                raise _Ambiguous(
                    f"a per-test result line appeared outside any block: {line[:120]!r}"
                )
            i += 1
    except _Ambiguous as exc:
        obs.ambiguous = str(exc)
        return
    if blocks == 0 and obs.exit not in (0, None):
        obs.collection_error = (
            "cargo test exited non-zero and no test binary reported 'running N tests'"
        )
