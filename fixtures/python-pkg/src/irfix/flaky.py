"""Decide whether the flaky fixture test fails this time.

With IR_FIXTURE_FLAKY_DIR set, a counter file there makes the test fail on
every second invocation: genuinely flaky across runs, deterministic for the
tool's own tests. Without it, the outcome follows the clock.
"""

import os
import time
from pathlib import Path


def should_fail():
    directory = os.environ.get("IR_FIXTURE_FLAKY_DIR")
    if directory:
        counter = Path(directory) / "python-flaky-counter"
        try:
            count = int(counter.read_text().strip())
        except (FileNotFoundError, ValueError):
            count = 0
        count += 1
        counter.write_text(str(count))
        return count % 2 == 0
    return time.time_ns() % 2 == 0
