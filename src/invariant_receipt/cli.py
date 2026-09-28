"""Command line (spec §8).

    invariant-receipt run inv.yaml [--repeat N] [--only NAME] [--timeout S] ...
    invariant-receipt render receipts/
    invariant-receipt --version

Exit codes, across every receipt: 1 if any SURVIVED (checked first), else 3 if
any UNKNOWN, else 2 if any INVALID, else 0. A run refused before anything was
tested (dirty tree, bad YAML, bad arguments) exits 4 and writes a summary
saying so — argparse's own usage-error code is 2, which would read as INVALID,
so it is remapped.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from . import engine, sandbox, spec
from . import summary as summ
from .identity import build_identity, describe
from .render import RenderError, clear_views, refused_summary, render, write_views

MARKER = ".invariant-receipt-output"


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # noqa: D401 - argparse API
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(summ.EXIT_REFUSED)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="invariant-receipt",
        description="Your test is green. Want a receipt showing it can turn red?",
    )
    p.add_argument("--version", action="store_true", help="print the build identity and exit")
    sub = p.add_subparsers(dest="command", parser_class=_Parser)
    r = sub.add_parser("run", help="run the explicit mutations in an invariants file")
    r.add_argument("spec", type=Path, help="invariants YAML file (inside a git repository)")
    r.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="baseline runs per invariant (mutations always run once)",
    )
    r.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="run only this invariant (repeatable)",
    )
    r.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds allowed for each build and each run (default 600)",
    )
    r.add_argument(
        "--out", type=Path, default=Path("receipts"), help="output directory (default ./receipts)"
    )
    r.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where disposable copies are made (default: system temp)",
    )
    r.add_argument("--keep", action="store_true", help="keep the disposable copies for inspection")
    r.add_argument(
        "--runner", choices=spec.RUNNERS, default=None, help="override every invariant's runner"
    )
    r.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter used to run pytest (default: this one)",
    )
    rr = sub.add_parser(
        "render",
        help="rebuild summary.json, receipt.md, receipt.html and pr-summary.txt from receipts",
    )
    rr.add_argument("out", type=Path, help="a receipts directory written by 'run'")
    return p


class Refused(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def prepare_out(out: Path) -> None:
    """Empty the output directory — but only one this tool created.

    Clearing it first is what stops a stale receipt from an earlier run being
    read as this run's result.
    """
    if out.exists() and not out.is_dir():
        raise Refused("bad_output_dir", f"{out} exists and is not a directory")
    if out.is_dir() and any(out.iterdir()):
        if not (out / MARKER).is_file():
            raise Refused(
                "bad_output_dir",
                f"{out} is not empty and was not created by invariant-receipt; refusing to delete it",
            )
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text(
        "Created by invariant-receipt. The next run empties this directory.\n"
    )


def _rel_inside(path: Path, root: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return None


def _write_refusal(out: Path, tool: dict, reason: str, detail: str) -> None:
    try:
        clear_views(out)
        write_views(out, refused_summary(tool, reason, detail), [], tool)
    except Exception:  # the refusal itself is still printed and exits 4
        traceback.print_exc()


def cmd_run(args, tool: dict) -> int:
    out = args.out.resolve()
    try:
        prepare_out(out)
    except (Refused, OSError) as exc:
        detail = exc.detail if isinstance(exc, Refused) else str(exc)
        print(f"invariant-receipt: refused: {detail}", file=sys.stderr)
        return summ.EXIT_REFUSED

    # ---- everything below writes its outcome into ``out`` ----------------
    try:
        if args.repeat < 1:
            raise Refused("bad_arguments", "--repeat must be at least 1")
        if args.timeout <= 0:
            raise Refused("bad_arguments", "--timeout must be positive")
        try:
            invariants = spec.load(args.spec)
        except spec.SpecError as exc:
            raise Refused("bad_spec", str(exc)) from exc
        if args.only:
            names = {i.name for i in invariants}
            missing = [n for n in args.only if n not in names]
            if missing:
                raise Refused("bad_arguments", f"--only names no invariant: {', '.join(missing)}")
            invariants = [i for i in invariants if i.name in args.only]
        try:
            repo = sandbox.repo_root(args.spec.resolve().parent)
            commit = sandbox.head_commit(repo)
        except (sandbox.SandboxError, OSError) as exc:
            raise Refused("not_a_repository", str(exc)) from exc
        for inv in invariants:
            if _rel_inside(inv.project_dir, repo) is None:
                raise Refused(
                    "bad_spec",
                    f"invariant {inv.name!r}: project {inv.project_dir} is outside {repo}",
                )
        excludes = [e for e in [_rel_inside(out, repo)] if e]
        if args.workdir is not None and _rel_inside(args.workdir, repo) is not None:
            raise Refused("bad_arguments", "--workdir must be outside the repository being tested")
        before = sandbox.tree_state(repo, excludes)
        if not before.clean:
            raise Refused(
                "dirty_tree",
                "the working tree has uncommitted or untracked changes; a receipt must name a real "
                "commit, so commit or stash them first: " + "; ".join(before.status[:20]),
            )
    except Refused as exc:
        print(f"invariant-receipt: refused ({exc.reason}): {exc.detail}", file=sys.stderr)
        _write_refusal(out, tool, exc.reason, exc.detail)
        return summ.EXIT_REFUSED
    except Exception:
        detail = traceback.format_exc(limit=6)
        print(f"invariant-receipt: internal error before any run:\n{detail}", file=sys.stderr)
        _write_refusal(out, tool, "internal_error", detail[-1500:])
        return summ.EXIT_UNKNOWN

    work, work_error = None, None
    try:
        base = args.workdir if args.workdir is not None else Path(tempfile.gettempdir())
        work = Path(tempfile.mkdtemp(prefix="invariant-receipt-", dir=base)).resolve()
    except OSError as exc:
        work_error = str(exc)

    ctx = engine.RunContext(
        repo=repo,
        commit=commit,
        out=out,
        work=work,
        work_error=work_error,
        keep=args.keep,
        repeat=args.repeat,
        timeout=args.timeout,
        runner_override=args.runner,
        python=args.python,
        tool=tool,
        excludes=excludes,
        before=before,
    )
    pending: list[engine.Pending] = []
    crash: str | None = None
    try:
        for inv in invariants:
            print(
                f"invariant-receipt: {inv.name} ({len(inv.mutations)} mutation(s))", file=sys.stderr
            )
            pending.extend(engine.run_invariant(ctx, inv))
    except BaseException as exc:  # includes Ctrl-C: still write what we know
        crash = "".join(traceback.format_exception(exc, limit=6))[-1500:]
        done = {(p.receipt["invariant"], p.receipt["mutation"]["id"]) for p in pending}
        for inv in invariants:
            for mut in inv.mutations:
                if (inv.name, mut.id) not in done:
                    pending.append(engine.stopped_receipt(ctx, inv, mut, crash))
    finally:
        if work is not None and not args.keep:
            sandbox.remove(work)

    after, after_error = None, None
    try:
        after = sandbox.tree_state(repo, excludes)
    except Exception as exc:
        after_error = str(exc)
    engine.finalize(pending, after, before, after_error)
    if after is not None and (after.hash != before.hash or after.status != before.status):
        print(
            "invariant-receipt: WARNING — THE ORIGINAL WORKING TREE CHANGED DURING THE RUN. "
            "Every receipt is UNKNOWN: source_tree_changed.",
            file=sys.stderr,
        )
    if ctx.kept:
        print(
            "invariant-receipt: kept copies:\n  " + "\n  ".join(map(str, ctx.kept)), file=sys.stderr
        )

    try:
        summary = render(out, tool)
    except Exception:
        traceback.print_exc()
        return (
            summ.EXIT_UNKNOWN
            if not any(p.receipt["verdict"] == "SURVIVED" for p in pending)
            else summ.EXIT_SURVIVED
        )
    for inv in summary["invariants"]:
        print(inv["pr_line"])
    if crash:
        print(f"invariant-receipt: the run stopped early:\n{crash}", file=sys.stderr)
    return summary["exit_code"]


def cmd_render(args, tool: dict) -> int:
    try:
        summary = render(args.out.resolve(), tool)
    except RenderError as exc:
        print(f"invariant-receipt: refused: {exc}", file=sys.stderr)
        return summ.EXIT_REFUSED
    for inv in summary["invariants"]:
        print(inv["pr_line"])
    return summary["exit_code"]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tool = build_identity()
    if args.version:
        print(describe(tool))
        return 0
    if args.command == "run":
        return cmd_run(args, tool)
    if args.command == "render":
        return cmd_render(args, tool)
    _parser().print_help(sys.stderr)
    return summ.EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
