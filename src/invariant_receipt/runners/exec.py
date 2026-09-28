"""The three runners (spec §6): pytest, cargo-nextest, cargo test.

Each runner knows how to check it is installed, how to build (compile step),
and how to run the one named test and turn the result into a RunObservation.
Nothing here decides a verdict.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from ..observation import RunObservation
from ..procs import ProcResult, run_logged
from ..verdict import CompileStep
from . import cargo_text, junit

NEXTEST_PROFILE = "invariant-receipt:run"
NEXTEST_RUN_ID_RE = re.compile(r"Nextest run ID ([0-9a-fA-F-]{36})")


class RunnerMissing(RuntimeError):
    pass


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:]


def _compile_step(proc: ProcResult, log_rel: str) -> tuple[CompileStep, dict]:
    if proc.spawn_error:
        step = CompileStep("failed", f"could not start the build: {proc.spawn_error}")
    elif proc.timed_out:
        step = CompileStep("timeout", f"build exceeded {proc.seconds}s")
    elif proc.exit != 0:
        step = CompileStep("failed", f"build exited {proc.exit}:\n{_tail(proc.output, 1500)}")
    else:
        step = CompileStep("ok", "")
    record = {
        "status": step.status,
        "command": proc.argv,
        "exit": proc.exit,
        "seconds": proc.seconds,
        "detail": step.detail,
        "log": log_rel,
    }
    return step, record


class Runner:
    kind = ""
    parser = ""

    def __init__(self, test: str, target: str | None, python: str):
        self.test = test
        self.target = target
        self.python = python
        self.version: str | None = None

    # -- environment -----------------------------------------------------
    def env(self, clone_root: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["CARGO_TERM_COLOR"] = "never"
        env["NO_COLOR"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Stable panic text: backtraces carry addresses and frame lists that
        # would make every failure signature unique.
        env["RUST_BACKTRACE"] = "0"
        # Each clone gets its own target dir, whatever the caller's
        # environment says. A shared target dir is NOT safe here: cargo's
        # artifact hash ignores the source path, so a second clone with older
        # mtimes can silently reuse the FIRST clone's (mutated) binary.
        # Observed while building this tool, 2026-09-28.
        env["CARGO_TARGET_DIR"] = str(clone_root / "target")
        env.pop("CARGO_BUILD_TARGET_DIR", None)
        return env

    def check(self, cwd: Path, env: dict, log_dir: Path, timeout: float) -> None:
        raise NotImplementedError

    def compile(
        self,
        cwd: Path,
        env: dict,
        log_path: Path,
        log_rel: str,
        timeout: float,
        changed: list[Path],
    ) -> tuple[CompileStep, dict]:
        raise NotImplementedError

    def run(
        self, cwd: Path, env: dict, log_path: Path, log_rel: str, timeout: float, work: Path
    ) -> RunObservation:
        raise NotImplementedError

    def _probe(self, argv: list[str], cwd: Path, env: dict, log_path: Path, timeout: float) -> str:
        proc = run_logged(argv, cwd, log_path, min(timeout, 120), env)
        if proc.spawn_error or proc.timed_out or proc.exit != 0:
            why = proc.spawn_error or ("timed out" if proc.timed_out else f"exit {proc.exit}")
            raise RunnerMissing(
                f"{' '.join(argv)} failed ({why}): {_tail(proc.output, 400).strip()}"
            )
        return proc.output.strip().splitlines()[-1] if proc.output.strip() else ""

    def _observation(self, proc: ProcResult, log_rel: str) -> RunObservation:
        return RunObservation(
            parser=self.parser,
            exit=proc.exit,
            timed_out=proc.timed_out,
            seconds=proc.seconds,
            command=proc.argv,
            log=log_rel,
            output_tail=_tail(proc.output),
        )


class PytestRunner(Runner):
    kind = "pytest"
    parser = "junit-xml"

    def check(self, cwd, env, log_dir, timeout):
        if shutil.which(self.python) is None and not Path(self.python).is_file():
            raise RunnerMissing(f"python interpreter {self.python!r} not found")
        self.version = "pytest " + self._probe(
            [self.python, "-c", "import pytest; print(pytest.__version__)"],
            cwd,
            env,
            log_dir / "runner-check.log",
            timeout,
        )

    def compile(self, cwd, env, log_path, log_rel, timeout, changed):
        py = [str(p) for p in changed if p.suffix == ".py" and p.is_file()]
        if not py:
            return CompileStep("skipped"), {
                "status": "skipped",
                "command": None,
                "exit": None,
                "seconds": None,
                "detail": "no Python files changed",
                "log": None,
            }
        # compile() only — nothing is written into the clone.
        code = "import sys\nfor f in sys.argv[1:]:\n    compile(open(f, 'rb').read(), f, 'exec')\n"
        proc = run_logged([self.python, "-c", code, *py], cwd, log_path, timeout, env)
        return _compile_step(proc, log_rel)

    def run(self, cwd, env, log_path, log_rel, timeout, work):
        xml = work / "junit.xml"
        if xml.exists():
            xml.unlink()
        argv = [
            self.python,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_family=xunit2",
            f"--junitxml={xml}",
            "-q",
            self.test,
        ]
        proc = run_logged(argv, cwd, log_path, timeout, env)
        obs = self._observation(proc, log_rel)
        if proc.spawn_error:
            obs.parse_error = f"could not start pytest: {proc.spawn_error}"
            return obs
        if proc.timed_out:
            return obs
        if not xml.is_file():
            if proc.exit in (2, 3, 4):
                obs.collection_error = f"pytest exited {proc.exit} and wrote no JUnit report"
            else:
                obs.parse_error = f"pytest exited {proc.exit} and wrote no JUnit report"
            return obs
        try:
            junit.parse_pytest(xml, obs, self.test)
        except junit.JUnitError as exc:
            obs.parse_error = str(exc)
        return obs


def _rust_target_args(target: str | None) -> list[str]:
    if target is None:
        return ["--tests"]
    if target == "lib":
        return ["--lib"]
    kind, name = target.split(":", 1)
    return [f"--{kind}", name]


class _CargoBase(Runner):
    def compile(self, cwd, env, log_path, log_rel, timeout, changed):
        argv = ["cargo", "test", "--no-run", *_rust_target_args(self.target)]
        proc = run_logged(argv, cwd, log_path, timeout, env)
        return _compile_step(proc, log_rel)

    def _cargo_version(self, cwd, env, log_dir, timeout) -> str:
        if shutil.which("cargo") is None:
            raise RunnerMissing("cargo not found on PATH")
        return self._probe(["cargo", "--version"], cwd, env, log_dir / "runner-check.log", timeout)


class CargoTextRunner(_CargoBase):
    kind = "cargo"
    parser = "cargo-text"

    def check(self, cwd, env, log_dir, timeout):
        self.version = self._cargo_version(cwd, env, log_dir, timeout)

    def run(self, cwd, env, log_path, log_rel, timeout, work):
        argv = ["cargo", "test", "--no-fail-fast", *_rust_target_args(self.target), self.test]
        proc = run_logged(argv, cwd, log_path, timeout, env)
        obs = self._observation(proc, log_rel)
        if proc.spawn_error:
            obs.parse_error = f"could not start cargo: {proc.spawn_error}"
            return obs
        if proc.timed_out:
            return obs
        try:
            cargo_text.parse(proc.output, obs)
        except cargo_text.CargoTextError as exc:
            obs.parse_error = str(exc)
        return obs


def nextest_available(cwd: Path, env: dict, log_path: Path) -> bool:
    if shutil.which("cargo") is None:
        return False
    proc = run_logged(["cargo", "nextest", "--version"], cwd, log_path, 60, env)
    return proc.exit == 0 and not proc.timed_out


class NextestRunner(_CargoBase):
    kind = "nextest"
    parser = "nextest-junit"

    def check(self, cwd, env, log_dir, timeout):
        cargo = self._cargo_version(cwd, env, log_dir, timeout)
        nx = self._probe(
            ["cargo", "nextest", "--version"],
            cwd,
            env,
            log_dir / "runner-check-nextest.log",
            timeout,
        )
        self.version = f"{nx.split(chr(10))[0]}; {cargo}"

    def _tool_config(self, work: Path) -> Path:
        cfg = work / "nextest-tool.toml"
        store = work / "nextest-store"
        cfg.write_text(
            "# Written by invariant-receipt for one run. A project's own\n"
            "# .config/nextest.toml outranks this file; see NextestRunner.run.\n"
            f"[store]\ndir = {str(store)!r}\n\n"
            f'[profile."{NEXTEST_PROFILE}"]\nretries = 0\nfail-fast = false\n\n'
            f'[profile."{NEXTEST_PROFILE}".junit]\npath = "junit.xml"\n'
            "store-success-output = true\nstore-failure-output = true\n",
            encoding="utf-8",
        )
        return cfg

    def _junit_candidates(self, cwd: Path, work: Path) -> list[Path]:
        cands = [work / "nextest-store" / NEXTEST_PROFILE / "junit.xml"]
        # A project config can move the store; honour it, read-only.
        for root in [cwd, *cwd.parents]:
            cfg = root / ".config" / "nextest.toml"
            if cfg.is_file():
                try:
                    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
                except (OSError, tomllib.TOMLDecodeError):
                    data = {}
                store = (data.get("store") or {}).get("dir", "target/nextest")
                sp = Path(store)
                sp = sp if sp.is_absolute() else root / sp
                cands.append(sp / NEXTEST_PROFILE / "junit.xml")
            if (root / ".git").exists():
                break
        return cands

    def run(self, cwd, env, log_path, log_rel, timeout, work):
        cfg = self._tool_config(work)
        argv = [
            "cargo",
            "nextest",
            "run",
            "--tool-config-file",
            f"invariant-receipt:{cfg}",
            "--profile",
            NEXTEST_PROFILE,
            # On the command line, because a project config outranks the tool
            # config: a retried failure that then passes is not one honest run.
            "--retries",
            "0",
            "--no-fail-fast",
            "--no-tests=warn",
            *_rust_target_args(self.target),
            self.test,
        ]
        proc = run_logged(argv, cwd, log_path, timeout, env)
        obs = self._observation(proc, log_rel)
        if proc.spawn_error:
            obs.parse_error = f"could not start cargo nextest: {proc.spawn_error}"
            return obs
        if proc.timed_out:
            return obs
        ids = NEXTEST_RUN_ID_RE.findall(proc.output)
        if len(ids) != 1:
            if proc.exit in (101, 104):
                obs.collection_error = (
                    f"cargo nextest exited {proc.exit} before starting a test run "
                    f"(build or test-list failure)"
                )
            else:
                obs.parse_error = (
                    f"expected one 'Nextest run ID' line in the output, found {len(ids)}"
                )
            return obs
        run_id = ids[0].lower()
        # Only a JUnit file stamped with THIS run's id is evidence. That rules
        # out a stale file from an earlier run wherever the store lives.
        for cand in self._junit_candidates(cwd, work):
            if not cand.is_file():
                continue
            try:
                uuid = (ET.parse(cand).getroot().get("uuid") or "").lower()
            except (OSError, ET.ParseError):
                continue
            if uuid == run_id:
                obs.notes.append(f"JUnit read from {cand} (uuid matches run id {run_id})")
                try:
                    junit.parse_nextest(cand, obs)
                except junit.JUnitError as exc:
                    obs.parse_error = str(exc)
                return obs
        obs.parse_error = f"no JUnit file stamped with this run's id {run_id} was found"
        return obs


def resolve(
    requested: str, test: str, target: str | None, python: str, cwd: Path, env: dict, log_dir: Path
) -> Runner:
    """Pick the runner. ``auto`` prefers nextest for Rust and records what it chose."""
    is_python = ".py::" in test or test.endswith(".py")
    is_rust = (cwd / "Cargo.toml").is_file()
    if requested == "pytest" or (requested == "auto" and is_python):
        if target is not None:
            raise RunnerMissing("'target' applies to Rust runners only")
        return PytestRunner(test, target, python)
    if requested in ("nextest", "cargo") or (requested == "auto" and is_rust):
        if is_python:
            raise RunnerMissing(f"{test!r} looks like a pytest node id, not a Rust test name")
        if requested == "nextest":
            return NextestRunner(test, target, python)
        if requested == "cargo":
            return CargoTextRunner(test, target, python)
        log_dir.mkdir(parents=True, exist_ok=True)
        if nextest_available(cwd, env, log_dir / "runner-probe-nextest.log"):
            return NextestRunner(test, target, python)
        return CargoTextRunner(test, target, python)
    raise RunnerMissing(
        f"runner 'auto' could not tell the language: {test!r} is not a pytest node id "
        f"and {cwd} has no Cargo.toml"
    )
