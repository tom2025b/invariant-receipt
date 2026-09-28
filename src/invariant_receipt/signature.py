"""Failure signatures (spec §4, amended 2026-09-28): WHERE the named test failed.

A signature is the named test plus the normalised location at which its
failure fired — the panic location for Rust, the crash location of the
traceback for pytest — as ``file:line``. The message is deliberately NOT part
of it: two breaks that trip the same assertion are one way, whatever values
the assertion message happens to print ("assert 2 == 1" and "assert 3 == 1"
are the same check failing).

When the runner's output gives no location, the fallback is the failure
message with every digit, hex value and quoted value stripped. The receipt
records which basis was used (``location`` or ``message-normalised``) and the
exact text that was hashed, so a reviewer can see what "same way" meant.
"""

from __future__ import annotations

import hashlib
import re

LOCATION = "location"
MESSAGE = "message-normalised"
KINDS = (LOCATION, MESSAGE)

# Rust: "thread 't' (123) panicked at src/lib.rs:88:14:" (column optional).
RUST_PANIC_RE = re.compile(r"panicked at (?P<loc>[^\n]*?:\d+(?::\d+)?):?\n")
# pytest long repr: the last "path:line: ExcType" line is where it fired.
PY_LOC_RE = re.compile(r"^(?P<loc>\S[^\n]*?:\d+): [A-Za-z_][\w.]*$", re.M)
_FILE_LINE_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?::\d+)?$")

_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"[ \t]+")


def _location(text: str) -> str | None:
    m = RUST_PANIC_RE.search(text)
    if m:
        return m["loc"]
    locs = list(PY_LOC_RE.finditer(text))
    if locs:
        return locs[-1]["loc"]
    return None


_DIFF_FILE_RE = re.compile(r"^\+\+\+ (?:b/(?P<path>.+)|/dev/null)$")
_HUNK_RE = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@")


def parse_diff(diff: str) -> dict[str, list[tuple[int, int, list[str]]]]:
    """Per changed file: its hunks as (old_start, new_start, body lines).

    A hunk header states how many old and new lines its body holds, and the
    body is consumed by COUNT, never by what a line looks like: a deleted
    source line "-- old" appears in the diff as "--- old", and an added "++ x"
    as "+++ x". Those are hunk content. File headers are only recognised
    outside a hunk body.
    """
    files: dict[str, list[tuple[int, int, list[str]]]] = {}
    current: list | None = None
    old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            tag = line[:1]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag in (" ", ""):
                old_left -= 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            elif tag == "+":
                new_left -= 1
            else:
                raise ValueError(f"unexpected line inside a diff hunk: {line[:80]!r}")
            if current is not None:
                current[-1][2].append(line if line else " ")
            continue
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = files.setdefault(m["path"], []) if m["path"] else None
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_left = int(m["ol"]) if m["ol"] is not None else 1
            new_left = int(m["nl"]) if m["nl"] is not None else 1
            if current is not None:
                current.append((int(m["os"]), int(m["ns"]), []))
    return files


def original_line(hunks: list[tuple[int, int, list[str]]], line: int) -> str:
    """Map a line number in the MUTATED file back to the unmutated file.

    Needed because a mutation that inserts lines above a test in the same
    file (routine for Rust unit tests) shifts that test's assertion down: the
    same assertion would otherwise look like a different place. A line the
    mutation itself inserted has no original; it is named relative to the
    original line it was inserted before.
    """
    delta = 0
    for old_start, new_start, body in hunks:
        if line < new_start:
            break
        o, n = old_start, new_start
        for b in body:
            tag = b[:1]
            if tag == " ":
                if n == line:
                    return str(o)
                o += 1
                n += 1
            elif tag == "-":
                o += 1
            elif tag == "+":
                if n == line:
                    return f"{o} (inside lines the mutation inserted)"
                n += 1
        delta = n - o
    return str(line - delta)


def normalise_location(loc: str, root: str | None = None, diff_files: dict | None = None) -> str:
    """``file:line`` relative to the copy's root, in the UNMUTATED file's numbering."""
    loc = loc.strip()
    if root:
        prefix = root.rstrip("/") + "/"
        if loc.startswith(prefix):
            loc = loc[len(prefix) :]
    m = _FILE_LINE_RE.match(loc)
    if not m:
        return loc
    file, line = m["file"], int(m["line"])
    for path, hunks in (diff_files or {}).items():
        if path == file or path.endswith("/" + file):
            return f"{file}:{original_line(hunks, line)}"
    return f"{file}:{line}"


def normalise_message(text: str) -> str:
    """The message with every quoted value, hex value and digit stripped."""
    text = _QUOTED.sub("<q>", text)
    text = _HEX.sub("<hex>", text)
    text = _DIGITS.sub("<n>", text)
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def basis(
    test: str,
    text: str,
    message: str | None = None,
    root: str | None = None,
    diff: str | None = None,
) -> tuple[str | None, str | None]:
    """(kind, normalised text to hash) for one failure of ``test``; (None, None) if there is nothing.

    ``diff`` is the mutation's own diff, used to express the location in the
    unmutated source's line numbers.
    """
    text = text or ""
    loc = _location(text)
    if loc is not None:
        where = normalise_location(loc, root, parse_diff(diff) if diff else None)
        return LOCATION, f"{test}\n@ {where}"
    msg = message if message else text
    if not msg.strip():
        return None, None
    if root:
        msg = msg.replace(root.rstrip("/") + "/", "")
    return MESSAGE, f"{test}\n{normalise_message(msg)}"


def signature(basis_text: str | None) -> str | None:
    if not basis_text:
        return None
    return "sha256:" + hashlib.sha256(basis_text.encode("utf-8")).hexdigest()
