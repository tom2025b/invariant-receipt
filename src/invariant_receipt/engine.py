"""Orchestration: baseline, then each mutation, each in its own disposable clone.

Honesty rules this module carries:

* **A crash still writes a receipt.** Every step is wrapped; an exception in the
  tool becomes ``UNKNOWN: internal_error`` in a receipt that is written, never
  a traceback with no receipt.
* **Receipts are provisional until the source tree is re-checked.** While the
  run is in progress each receipt on disk says UNKNOWN. Only after the working
  tree is proven unchanged does the real verdict replace it. A run that dies
  half way leaves UNKNOWN receipts behind, never unconfirmed CAUGHTs.
* **One fresh clone per mutation.** A mutation can never leak into the next
  run through a shared checkout or a shared build directory.
"""

from __future__ import annotations

import hashlib
import subprocess
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import sandbox, signature
from .observation import RunObservation
from .receipt import RECEIPT_SCHEMA_ID, validate_receipt, write_json
from .runners import exec as runners
from .spec import Invariant, Mutation
from .verdict import (
    UNKNOWN,
    Decision,
    Evidence,
    MutationFacts,
    baseline_problem,
    decide,
)

EXCERPT_CHARS = 4000
DIFF_CHARS = 20000


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _crash(where: str) -> tuple[str, str]:
    tb = traceback.format_exc(limit=6)
    return "internal_error", f"{where}: {tb[-1500:]}"


@dataclass
class RunContext:
    repo: Path
    commit: str
    out: Path
    work: Path | None  # None: the scratch directory could not be created
    work_error: str | None
    keep: bool
    repeat: int
    timeout: float
    runner_override: str | None
    python: str
    tool: dict
    excludes: list[str]
    before: sandbox.TreeState
    kept: list[Path] = field(default_factory=list)


@dataclass
class Pending:
    """A receipt whose final verdict waits on the after-run tree check."""

    path: Path
    receipt: dict
    decision: Decision


def _overlapping_count(text: str, needle: str) -> int:
    n, start = 0, 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return n
        n += 1
        start = i + 1


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def apply_mutation(
    clone_root: Path, project_rel: str, mut: Mutation, block: dict
) -> tuple[MutationFacts, list[Path]]:
    """Apply ``mut`` in the clone. Fills ``block``; returns facts and changed files."""
    proj = clone_root / project_rel
    if mut.kind == "find_replace":
        path = proj / mut.file
        if not _inside(path, clone_root):
            return MutationFacts(False, False, f"{mut.file} resolves outside the copy"), []
        if not path.is_file():
            return MutationFacts(
                False, False, f"{mut.file} does not exist at the committed HEAD"
            ), []
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return MutationFacts(False, False, f"{mut.file} is not UTF-8 text"), []
        n = _overlapping_count(text, mut.find)
        if n != 1:
            return MutationFacts(
                False, False, f"'find' matched {n} times in {mut.file}; it must match exactly once"
            ), []
        path.write_bytes(text.replace(mut.find, mut.replace, 1).encode("utf-8"))
    else:
        patch_bytes = mut.patch_path.read_bytes()
        block["patch_sha256"] = "sha256:" + hashlib.sha256(patch_bytes).hexdigest()
        extra = ["--directory", project_rel] if project_rel not in ("", ".") else []
        check = sandbox.git(["apply", "--check", *extra, str(mut.patch_path)], clone_root)
        if check.returncode != 0:
            why = check.stderr.decode(errors="replace").strip()
            return MutationFacts(False, False, f"patch does not apply cleanly: {why}"), []
        done = sandbox.git(["apply", *extra, str(mut.patch_path)], clone_root)
        if done.returncode != 0:
            why = done.stderr.decode(errors="replace").strip()
            return MutationFacts(False, False, f"git apply failed: {why}"), []
    sandbox.stage_all(clone_root)
    diff = sandbox.diff(clone_root)
    changed = bool(diff.strip())
    block["diff"] = diff[:DIFF_CHARS] if changed else None
    block["fingerprint"] = (
        "sha256:" + hashlib.sha256(diff.encode("utf-8")).hexdigest() if changed else None
    )
    files = [clone_root / p for p in sandbox.changed_files(clone_root)] if changed else []
    detail = "" if changed else "the edit left every file byte-for-byte identical"
    return MutationFacts(True, changed, detail), files


def _run_block(
    obs: RunObservation, test: str, decision: Decision, root: str | None, diff: str | None
) -> dict:
    block = obs.to_json(test)
    result, case, _n = obs.named(test)
    block["named_test_observed"] = result in ("passed", "failed")
    block["named_test_result"] = result
    block["oracle_matched"] = decision.oracle_matched
    kind, basis = None, None
    if case is not None and result == "failed":
        kind, basis = signature.basis(test, case.text, case.message, root, diff)
    block["failure_signature_kind"] = kind
    block["failure_signature_basis"] = basis
    block["failure_signature"] = signature.signature(basis)
    block["failure_excerpt"] = case.text[:EXCERPT_CHARS] if case is not None and case.text else None
    return block


def _base_env(ctx: RunContext, clone_root: Path) -> dict:
    return runners.Runner("", None, ctx.python).env(clone_root)


def _project_rel(ctx: RunContext, inv: Invariant) -> str:
    return str(inv.project_dir.relative_to(ctx.repo)) if inv.project_dir != ctx.repo else "."


def _empty_mblock(mut: Mutation) -> dict:
    return {
        "id": mut.id,
        "description": mut.description,
        "kind": mut.kind,
        "file": mut.file,
        "find": mut.find,
        "replace": mut.replace,
        "patch": mut.patch,
        "patch_sha256": None,
        "fingerprint": None,
        "diff": None,
        "applied": None,
        "changed": None,
        "apply_detail": "",
        "compile": {"status": "not_run"},
        "run": None,
    }


def _make_pending(
    ctx: RunContext,
    inv: Invariant,
    project_rel: str,
    runner_block: dict,
    baseline_block: dict,
    mblock: dict,
    decision: Decision,
    started: str,
) -> Pending:
    """Write the provisional receipt (UNKNOWN until finalize) and remember the decision."""
    receipt = {
        "schema": RECEIPT_SCHEMA_ID,
        "tool": ctx.tool,
        "invariant": inv.name,
        "description": inv.description,
        "test": inv.test,
        "expect": {"outcome": "fail", "matches": None},
        "source": {
            "repo": str(ctx.repo),
            "repo_commit": ctx.commit,
            "project": project_rel,
            "tree_clean_before": ctx.before.clean,
            "tree_status_before": ctx.before.status,
            "tree_hash_before": ctx.before.hash,
            "tree_clean_after": None,
            "tree_status_after": None,
            "tree_hash_after": None,
            "tree_unchanged": None,
        },
        "runner": dict(runner_block),
        "baseline": baseline_block,
        "mutation": mblock,
        "verdict": UNKNOWN,
        "reason": "internal_error",
        "reason_detail": (
            f"provisional: the run has not finished its source-tree check "
            f"(pending verdict {decision.verdict}: {decision.reason})"
        ),
        "started_at": started,
        "finished_at": now(),
    }
    mut = next(m for m in inv.mutations if m.id == mblock["id"])
    receipt["expect"] = {"outcome": mut.expect.outcome, "matches": mut.expect.matches}
    path = ctx.out / inv.name / mblock["id"] / "receipt.json"
    write_json(path, receipt)
    return Pending(path, receipt, decision)


def stopped_receipt(ctx: RunContext, inv: Invariant, mut: Mutation, why: str) -> Pending:
    """A receipt for a mutation the tool never reached because it stopped early."""
    runner_block = {
        "requested": ctx.runner_override or inv.runner,
        "kind": None,
        "parser": None,
        "version": None,
        "target": inv.target,
    }
    baseline_block = {
        "repeat": ctx.repeat,
        "compile": {"status": "not_run"},
        "runs": [],
        "status": "not_run",
    }
    decision = Decision(UNKNOWN, "internal_error", f"not run: the tool stopped early. {why}")
    return _make_pending(
        ctx,
        inv,
        _project_rel(ctx, inv),
        runner_block,
        baseline_block,
        _empty_mblock(mut),
        decision,
        now(),
    )


def run_invariant(ctx: RunContext, inv: Invariant) -> list[Pending]:
    project_rel = _project_rel(ctx, inv)
    inv_out = ctx.out / inv.name
    base_out = inv_out / "baseline"
    requested = ctx.runner_override or inv.runner

    runner: runners.Runner | None = None
    runner_block = {
        "requested": requested,
        "kind": None,
        "parser": None,
        "version": None,
        "target": inv.target,
    }
    baseline_block: dict = {
        "repeat": ctx.repeat,
        "compile": {"status": "not_run"},
        "runs": [],
        "status": "not_run",
    }
    ev_base = Evidence(test=inv.test, matches=None)

    # -- baseline ---------------------------------------------------------
    clone: Path | None = None
    try:
        if ctx.work is None:
            ev_base.tool_error = (
                "copy_failed",
                f"could not create a scratch directory: {ctx.work_error}",
            )
        else:
            clone = ctx.work / f"{inv.name}--baseline"
            try:
                sandbox.clone(ctx.repo, ctx.commit, clone)
            except (sandbox.SandboxError, OSError, subprocess.SubprocessError) as exc:
                ev_base.tool_error = ("copy_failed", f"baseline copy: {exc}")
        if ev_base.tool_error is None:
            cwd = clone / project_rel
            env = _base_env(ctx, clone)
            try:
                runner = runners.resolve(
                    requested, inv.test, inv.target, ctx.python, cwd, env, base_out
                )
                env = runner.env(clone)
                runner.check(cwd, env, base_out, ctx.timeout)
            except runners.RunnerMissing as exc:
                ev_base.tool_error = ("runner_missing", str(exc))
            if runner is not None:
                runner_block.update(kind=runner.kind, parser=runner.parser, version=runner.version)
        if ev_base.tool_error is None:
            step, rec = runner.compile(
                cwd,
                env,
                base_out / "compile.log",
                f"{inv.name}/baseline/compile.log",
                ctx.timeout,
                [],
            )
            ev_base.baseline_compile = step
            baseline_block["compile"] = rec
            if step.status in ("ok", "skipped"):
                for i in range(1, ctx.repeat + 1):
                    work = ctx.work / f"{inv.name}--baseline-work-{i}"
                    work.mkdir(parents=True, exist_ok=True)
                    obs = runner.run(
                        cwd,
                        env,
                        base_out / f"run-{i}.log",
                        f"{inv.name}/baseline/run-{i}.log",
                        ctx.timeout,
                        work,
                    )
                    ev_base.baseline_runs.append(obs)
                    baseline_block["runs"].append(obs.to_json(inv.test))
                    if baseline_problem(ev_base) is not None:
                        break  # one bad repeat is enough; the baseline is not green
    except Exception:
        ev_base.tool_error = _crash("baseline")
    finally:
        if clone is not None:
            if ctx.keep:
                ctx.kept.append(clone)
            else:
                sandbox.remove(clone)

    base_ok = ev_base.tool_error is None and baseline_problem(ev_base) is None
    if ev_base.baseline_runs:
        baseline_block["status"] = "green" if base_ok else "not_green"

    # -- mutations ---------------------------------------------------------
    pending: list[Pending] = []
    for mut in inv.mutations:
        started = now()
        mblock = _empty_mblock(mut)
        ev = Evidence(
            test=inv.test,
            matches=mut.expect.matches,
            tool_error=ev_base.tool_error,
            baseline_compile=ev_base.baseline_compile,
            baseline_runs=ev_base.baseline_runs,
        )
        mclone: Path | None = None
        obs: RunObservation | None = None
        full_diff: str | None = None
        try:
            if base_ok:
                mut_out = inv_out / mut.id
                mclone = ctx.work / f"{inv.name}--{mut.id}"
                try:
                    sandbox.clone(ctx.repo, ctx.commit, mclone)
                except (sandbox.SandboxError, OSError, subprocess.SubprocessError) as exc:
                    ev.tool_error = ("copy_failed", f"mutation copy: {exc}")
                if ev.tool_error is None:
                    facts, changed_files = apply_mutation(mclone, project_rel, mut, mblock)
                    full_diff = sandbox.diff(mclone) if facts.changed else None
                    mblock.update(
                        applied=facts.applied,
                        changed=facts.changed,
                        apply_detail=facts.apply_detail,
                    )
                    if facts.applied and facts.changed:
                        cwd = mclone / project_rel
                        env = runner.env(mclone)
                        step, rec = runner.compile(
                            cwd,
                            env,
                            mut_out / "compile.log",
                            f"{inv.name}/{mut.id}/compile.log",
                            ctx.timeout,
                            changed_files,
                        )
                        facts.compile = step
                        mblock["compile"] = rec
                        if step.status in ("ok", "skipped"):
                            work = ctx.work / f"{inv.name}--{mut.id}-work"
                            work.mkdir(parents=True, exist_ok=True)
                            # Exactly one run per mutation (spec §8): --repeat is baseline-only.
                            obs = runner.run(
                                cwd,
                                env,
                                mut_out / "run.log",
                                f"{inv.name}/{mut.id}/run.log",
                                ctx.timeout,
                                work,
                            )
                            facts.run = obs
                    ev.mutation = facts
            decision = decide(ev)
        except Exception:
            reason, detail = _crash(f"mutation {mut.id!r}")
            decision = Decision(UNKNOWN, reason, detail)
        finally:
            if mclone is not None:
                if ctx.keep:
                    ctx.kept.append(mclone)
                else:
                    sandbox.remove(mclone)

        try:
            if obs is not None:
                mblock["run"] = _run_block(
                    obs, inv.test, decision, str(mclone) if mclone else None, full_diff
                )
        except Exception:
            reason, detail = _crash(f"recording mutation {mut.id!r}")
            decision = Decision(UNKNOWN, reason, detail)

        pending.append(
            _make_pending(
                ctx, inv, project_rel, runner_block, baseline_block, mblock, decision, started
            )
        )
    return pending


def finalize(
    pending: list[Pending],
    after: sandbox.TreeState | None,
    before: sandbox.TreeState,
    after_error: str | None = None,
) -> None:
    """Apply the after-run tree check to every receipt, validate, and rewrite it."""
    unchanged = after is not None and after.hash == before.hash and after.status == before.status
    for p in pending:
        r = p.receipt
        src = r["source"]
        if after is not None:
            src.update(
                tree_clean_after=after.clean,
                tree_status_after=after.status,
                tree_hash_after=after.hash,
                tree_unchanged=unchanged,
            )
        d = p.decision
        if after is None:
            r["verdict"], r["reason"] = UNKNOWN, "internal_error"
            r["reason_detail"] = (
                f"the source tree could not be re-checked after the run: {after_error}"
            )
        elif not unchanged:
            r["verdict"], r["reason"] = UNKNOWN, "source_tree_changed"
            r["reason_detail"] = (
                "THE ORIGINAL WORKING TREE CHANGED DURING THE RUN. Nothing this run observed "
                f"can be trusted. (Before the tree check the verdict would have been "
                f"{d.verdict}: {d.reason}.)"
            )
        else:
            r["verdict"], r["reason"], r["reason_detail"] = d.verdict, d.reason, d.detail
        try:
            validate_receipt(r)
        except Exception as exc:  # the tool built a receipt its own schema rejects
            r["verdict"], r["reason"] = UNKNOWN, "internal_error"
            r["reason_detail"] = f"receipt failed schema validation: {str(exc)[:1500]}"
            validate_receipt(r)
        write_json(p.path, r)
