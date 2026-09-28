"""Failure signatures (spec §4): a normalised fingerprint of HOW the named test failed.

Two breaks that trip the same assertion with the same message are one way,
not two. The signature is built from the failure's location plus its message,
with volatile parts removed: addresses, absolute paths, timings, thread ids.
The normalised text that was hashed is kept in the receipt so a reviewer can
see exactly what "same way" meant.
"""

from __future__ import annotations

import hashlib
import re

RUST_PANIC_RE = re.compile(
    r"panicked at (?P<loc>[^\n]+?):\n(?P<msg>.*?)(?:\nnote: |\nstack backtrace:|\n\s*\n|\Z)",
    re.S,
)
PY_LOC_RE = re.compile(r"^(?P<loc>\S[^\n]*?:\d+): (?P<exc>[A-Za-z_][\w.]*)$", re.M)

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_THREAD_ID = re.compile(r"(thread '[^']*') \(\d+\)")
_ABS_PATH = re.compile(r"(?:/[^/\s:'\"()<>]+)+/([^/\s:'\"()<>]+)")
_TIMING = re.compile(r"\b\d+(?:\.\d+)?\s?(?:s|ms|us|µs|ns)\b")
_WS = re.compile(r"[ \t]+")


def normalise(text: str) -> str:
    text = _THREAD_ID.sub(r"\1", text)
    text = _HEX.sub("0x?", text)
    text = _ABS_PATH.sub(r"\1", text)
    text = _TIMING.sub("<time>", text)
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def basis(text: str, message: str | None = None) -> str | None:
    """The normalised "location | message" string for one failure, or None."""
    if not text and not message:
        return None
    m = RUST_PANIC_RE.search(text or "")
    if m:
        return normalise(f"{m['loc']}\n{m['msg']}")
    locs = list(PY_LOC_RE.finditer(text or ""))
    if locs:
        loc = locs[-1]
        msg = message if message is not None else ""
        return normalise(f"{loc['loc']}: {loc['exc']}\n{msg}")
    return normalise(f"{message or ''}\n{text or ''}")


def signature(basis_text: str | None) -> str | None:
    if not basis_text:
        return None
    return "sha256:" + hashlib.sha256(basis_text.encode("utf-8")).hexdigest()
