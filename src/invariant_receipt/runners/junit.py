"""JUnit XML parsing for pytest (``--junitxml``) and cargo-nextest."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..observation import RunObservation, TestCase

PYTEST_TIMEOUT_RE = re.compile(r"^Failed: Timeout\b.*pytest-timeout", re.S)


class JUnitError(ValueError):
    pass


def _read(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise JUnitError(f"cannot read JUnit XML {path}: {exc}") from exc


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    parts = []
    if el.get("message"):
        parts.append(el.get("message", ""))
    if el.text:
        parts.append(el.text)
    return "\n".join(parts)


def _cases(root: ET.Element):
    if root.tag == "testcase":
        yield root
        return
    yield from root.iter("testcase")


def pytest_nodeid_to_case(nodeid: str) -> tuple[str, str]:
    """(classname, name) that pytest's JUnit writer uses for ``nodeid``.

    Mirrors ``_pytest.junitxml.mangle_test_address``.
    """
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def parse_pytest(path: Path, obs: RunObservation, nodeid: str) -> None:
    """Fill ``obs`` from a pytest JUnit file. Raises JUnitError if unreadable."""
    root = _read(path)
    want = pytest_nodeid_to_case(nodeid)
    for tc in _cases(root):
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if error is not None and error.get("message", "").startswith("collection failure"):
            obs.collection_error = f"{name}: {error.get('message')}\n{error.text or ''}".strip()
            continue
        message = None
        if failure is not None:
            text, message = _text(failure), failure.get("message")
            result = "timeout" if PYTEST_TIMEOUT_RE.match(failure.get("message", "")) else "failed"
        elif error is not None:
            result, text, message = "error", _text(error), error.get("message")
        elif skipped is not None:
            result, text = "skipped", _text(skipped)
        else:
            result, text = "passed", ""
        # Cases are keyed by the user's node id when they are the named test,
        # so ``RunObservation.named`` matches on exactly what was written.
        ident = nodeid if (classname, name) == want else f"{classname}::{name}"
        obs.cases.append(
            TestCase(name=ident, result=result, group=classname, text=text, message=message)
        )


NEXTEST_RERUN_TAGS = ("rerunFailure", "rerunError", "flakyFailure", "flakyError")


def parse_nextest(path: Path, obs: RunObservation) -> None:
    """Fill ``obs`` from a cargo-nextest JUnit file. Raises JUnitError if unreadable."""
    root = _read(path)
    for suite in root.iter("testsuite"):
        binary = suite.get("name", "")
        for tc in suite.iter("testcase"):
            for tag in NEXTEST_RERUN_TAGS:
                if tc.find(tag) is not None:
                    # Retries were supposed to be off. A result that needed a
                    # retry cannot be read as a single honest observation.
                    raise JUnitError(
                        f"{tc.get('name')}: JUnit has <{tag}> although retries were disabled"
                    )
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            out = tc.findtext("system-out") or ""
            err = tc.findtext("system-err") or ""
            if failure is not None or error is not None:
                el = failure if failure is not None else error
                kind = (el.get("type") or "").lower()
                if "timeout" in kind:
                    result = "timeout"
                elif "exec fail" in kind or error is not None:
                    result = "error"
                else:
                    result = "failed"
                text = "\n".join(x for x in (_text(el), err, out) if x)
            elif skipped is not None:
                result, text = "skipped", _text(skipped)
            else:
                result, text = "passed", ""
            obs.cases.append(
                TestCase(name=tc.get("name", ""), result=result, group=binary, text=text)
            )
