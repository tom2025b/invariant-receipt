"""Preflight an invariants file without applying any mutation.

Validation deliberately reads the author's current YAML and patch files, but
checks them against a disposable clone of committed HEAD.  That makes the
command useful before committing while keeping discovery/build by-products out
of the source tree.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox
from .runners import exec as runners
from .spec import Invariant, Mutation


@dataclass
class ValidationResult:
    invariant: str
    runner: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _overlapping_count(text: str, needle: str) -> int:
    count, start = 0, 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return count
        count += 1
        start = found + 1


def check_mutation(clone_root: Path, project_rel: str, mutation: Mutation) -> str | None:
    """Return an authoring error, or None when the declared edit is applicable."""
    project = clone_root / project_rel
    if mutation.kind == "find_replace":
        path = project / mutation.file
        if not _inside(path, clone_root):
            return f"{mutation.file} resolves outside the committed checkout"
        if not path.is_file():
            return f"{mutation.file} does not exist at committed HEAD"
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"{mutation.file} is not UTF-8 text"
        except OSError as exc:
            return f"cannot read {mutation.file}: {exc}"
        matches = _overlapping_count(text, mutation.find)
        if matches != 1:
            return (
                f"'find' matched {matches} times in {mutation.file} at committed HEAD; "
                "it must match exactly once"
            )
        if text.replace(mutation.find, mutation.replace, 1) == text:
            return "find and replace leave the file byte-for-byte identical"
        return None

    extra = ["--directory", project_rel] if project_rel not in ("", ".") else []
    proc = sandbox.git(["apply", "--check", *extra, str(mutation.patch_path)], clone_root)
    if proc.returncode:
        detail = proc.stderr.decode(errors="replace").strip()
        return f"patch does not apply cleanly at committed HEAD: {detail}"
    return None


def validate_invariant(
    clone_root: Path,
    repo: Path,
    invariant: Invariant,
    runner_override: str | None,
    python: str,
    timeout: float,
    work: Path,
) -> ValidationResult:
    result = ValidationResult(invariant.name)
    project_rel = (
        str(invariant.project_dir.relative_to(repo)) if invariant.project_dir != repo else "."
    )
    project = clone_root / project_rel
    if not _inside(project, clone_root) or not project.is_dir():
        result.errors.append(f"project {invariant.project!r} is absent from committed HEAD")
        return result

    for mutation in invariant.mutations:
        error = check_mutation(clone_root, project_rel, mutation)
        if error:
            result.errors.append(f"mutation {mutation.id!r}: {error}")

    requested = runner_override or invariant.runner
    env = runners.Runner("", None, python).env(clone_root)
    logs = work / invariant.name
    logs.mkdir(parents=True, exist_ok=True)
    try:
        runner = runners.resolve(
            requested, invariant.test, invariant.target, python, project, env, logs
        )
        runner.check(project, env, logs, timeout)
        result.runner = runner.kind
        observation = runner.run(
            project,
            env,
            logs / "named-test.log",
            "named-test.log",
            timeout,
            logs,
        )
    except runners.RunnerMissing as exc:
        result.errors.append(f"runner {requested!r}: {exc}")
        return result

    named, _case, matches = observation.named(invariant.test)
    if observation.timed_out:
        result.errors.append(f"named-test discovery timed out after {observation.seconds}s")
    elif observation.collection_error:
        result.errors.append(f"named-test discovery failed: {observation.collection_error}")
    elif observation.parse_error:
        result.errors.append(f"named-test discovery was ambiguous: {observation.parse_error}")
    elif matches != 1 or named not in ("passed", "failed"):
        result.errors.append(
            f"named test {invariant.test!r} was observed {matches} times (expected exactly once)"
        )
    elif named != "passed":
        result.errors.append(f"named test {invariant.test!r} is {named} on unmodified HEAD")
    return result


def validate_all(
    repo: Path,
    commit: str,
    invariants: list[Invariant],
    runner_override: str | None,
    python: str,
    timeout: float,
    workdir: Path | None,
    keep: bool,
) -> tuple[list[ValidationResult], Path | None]:
    base = workdir if workdir is not None else Path(tempfile.gettempdir())
    work = Path(tempfile.mkdtemp(prefix="invariant-receipt-validate-", dir=base)).resolve()
    clone_root = work / "repo"
    try:
        sandbox.clone(repo, commit, clone_root)
        results = [
            validate_invariant(clone_root, repo, invariant, runner_override, python, timeout, work)
            for invariant in invariants
        ]
    except Exception:
        if not keep:
            sandbox.remove(work)
        raise
    if keep:
        return results, work
    sandbox.remove(work)
    return results, None
