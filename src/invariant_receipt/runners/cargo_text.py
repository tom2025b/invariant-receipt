"""Parse ``cargo test`` human-readable output (the fallback when nextest is absent).

``cargo test``'s JSON output is nightly-only, so stable Rust leaves text. This
parser is deliberately suspicious: every ``test result:`` summary is checked
against the per-test lines it follows, and a mismatch is a parse error (the
tool's evidence is unreadable -> UNKNOWN), never a guess.

It expects stdout and stderr merged in order, as ``procs.run_logged`` records
them, so "Running <binary>" lines land before that binary's results.
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


def parse(output: str, obs: RunObservation) -> None:
    """Fill ``obs`` from merged ``cargo test`` output. Raises CargoTextError."""
    lines = output.splitlines()
    binary = ""
    block: dict | None = None
    blocks = 0
    failure_text: dict[tuple[str, str], list[str]] = {}
    capturing: tuple[str, str] | None = None

    def close_block(summary: re.Match | None) -> None:
        nonlocal block
        assert block is not None
        seen = block["cases"]
        if summary is None:
            obs.notes.append(
                f"{block['binary'] or 'a test binary'}: 'running {block['expected']} tests' "
                f"had no 'test result:' line — the binary stopped early; "
                f"{len(seen)} result line(s) were read"
            )
        else:
            passed = sum(1 for c in seen if c.result == "passed")
            failed = sum(1 for c in seen if c.result == "failed")
            ignored = sum(1 for c in seen if c.result == "skipped")
            want = (int(summary["passed"]), int(summary["failed"]), int(summary["ignored"]))
            if (passed, failed, ignored) != want or len(seen) != block["expected"]:
                raise CargoTextError(
                    f"{block['binary']}: summary says {want[0]} passed/{want[1]} failed/"
                    f"{want[2]} ignored of {block['expected']}, but the per-test lines show "
                    f"{passed}/{failed}/{ignored} of {len(seen)}"
                )
        obs.cases.extend(seen)
        block = None

    for line in lines:
        m = RUNNING_RE.match(line)
        if m and capturing is None:
            if block is not None:
                # The previous binary never printed its summary: it crashed.
                close_block(None)
            binary = m["what"]
            continue
        m = BLOCK_RE.match(line)
        if m:
            if block is not None:
                close_block(None)
            block = {"binary": binary, "expected": int(m["n"]), "cases": []}
            blocks += 1
            capturing = None
            continue
        if block is None:
            continue
        m = RESULT_RE.match(line)
        if m:
            capturing = None
            close_block(m)
            continue
        m = STDOUT_HDR_RE.match(line)
        if m:
            capturing = (binary, m["name"])
            failure_text[capturing] = []
            continue
        if line == "failures:":
            capturing = None
            continue
        if capturing is not None:
            failure_text[capturing].append(line)
            continue
        if SLOW_RE.match(line):
            continue
        m = TEST_RE.match(line)
        if m:
            res = m["res"]
            result = "passed" if res == "ok" else "failed" if res == "FAILED" else "skipped"
            block["cases"].append(TestCase(name=m["name"], result=result, group=binary))
    if block is not None:
        close_block(None)

    for case in obs.cases:
        text = failure_text.get((case.group, case.name))
        if text is not None:
            case.text = "\n".join(text).strip("\n")

    if blocks == 0 and obs.exit not in (0, None):
        obs.collection_error = (
            "cargo test exited non-zero and no test binary reported 'running N tests'"
        )
