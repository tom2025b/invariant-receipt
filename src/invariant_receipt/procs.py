"""Running external commands with a hard timeout, output kept on disk.

Every command the tool runs goes through ``run_logged``. It never raises for
the command's own failure: a missing binary, a timeout and a non-zero exit all
come back as data, so the caller can put them in a receipt.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

TAIL_BYTES = 6000


@dataclass
class ProcResult:
    argv: list[str]
    cwd: str
    exit: int | None
    timed_out: bool
    seconds: float
    log_path: Path
    output: str
    spawn_error: str | None = None

    @property
    def tail(self) -> str:
        return self.output[-TAIL_BYTES:]


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_logged(
    argv: list[str],
    cwd: Path,
    log_path: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> ProcResult:
    """Run ``argv`` in its own process group, stdout+stderr merged into ``log_path``.

    The merged stream keeps cargo's "Running <binary>" lines (stderr) in order
    with the test harness output (stdout), which the cargo-text parser needs.
    On timeout the whole process group is killed, so a hung test binary cannot
    outlive the tool.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return ProcResult(
                argv=list(argv),
                cwd=str(cwd),
                exit=None,
                timed_out=False,
                seconds=round(time.monotonic() - start, 3),
                log_path=log_path,
                output="",
                spawn_error=f"{type(exc).__name__}: {exc}",
            )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        else:
            # The direct child exited; make sure nothing it spawned lingers.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    seconds = round(time.monotonic() - start, 3)
    output = log_path.read_bytes().decode("utf-8", errors="replace")
    return ProcResult(
        argv=list(argv),
        cwd=str(cwd),
        exit=None if timed_out else proc.returncode,
        timed_out=timed_out,
        seconds=seconds,
        log_path=log_path,
        output=output,
    )
