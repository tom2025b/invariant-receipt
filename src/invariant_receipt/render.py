"""Human views of the receipts (spec §8): receipt.md, receipt.html, pr-summary.txt.

Everything here is computed from the receipt.json files on disk, after they
have been validated against the schema. The views never see anything the JSON
does not record, so they cannot say more than the evidence does.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from . import summary as summ
from .identity import describe
from .receipt import read_json, validate_receipt, validate_summary, write_json

VIEWS = ("summary.json", "receipt.md", "receipt.html", "pr-summary.txt")


class RenderError(RuntimeError):
    pass


def load_receipts(out: Path) -> list[dict]:
    receipts = []
    for path in sorted(out.glob("*/*/receipt.json")):
        try:
            data = read_json(path)
            validate_receipt(data)
        except Exception as exc:
            raise RenderError(f"{path} is not a valid receipt: {str(exc)[:600]}") from exc
        receipts.append(data)
    return receipts


def _short(sha: str | None, n: int = 12) -> str:
    if not sha:
        return "—"
    return sha.split(":", 1)[-1][:n]


def _baseline_line(r: dict) -> str:
    b = r["baseline"]
    runs = b["runs"]
    if b["status"] == "not_run" or not runs:
        return f"not run (compile: {b['compile']['status']})"
    green = sum(1 for x in runs if x["named_test"] == "passed")
    word = "green" if b["status"] == "green" else "NOT green"
    return (
        f"{word} — named test passed on {green} of {len(runs)} run(s) executed "
        f"(repeat requested: {b['repeat']}); discovered per run: "
        + ", ".join(str(x["discovered"]) for x in runs)
    )


def _tree_line(r: dict) -> str:
    s = r["source"]
    if s["tree_unchanged"] is True:
        return f"unchanged before and after (tracked-file hash {_short(s['tree_hash_before'])}, git status clean)"
    if s["tree_unchanged"] is False:
        return (
            f"CHANGED DURING THE RUN — hash {_short(s['tree_hash_before'])} → {_short(s['tree_hash_after'])}; "
            f"status after: {', '.join(s['tree_status_after'] or []) or 'clean'}"
        )
    return "not re-checked (the run did not finish)"


def _edit_lines(m: dict) -> list[str]:
    if m["kind"] == "patch":
        return [f"patch `{m['patch']}` (sha256 {_short(m.get('patch_sha256'))})"]
    return [f"`{m['file']}`: replace `{m['find']}` with `{m['replace']}`"]


# ---------------------------------------------------------------- markdown --


def _md_code(text: str, lang: str = "text") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{lang}\n{text.rstrip()}\n{fence}"


def markdown(summary: dict, receipts: list[dict]) -> str:
    by = {}
    for r in receipts:
        by.setdefault(r["invariant"], []).append(r)
    tool = summary["tool"]
    lines = ["# Invariant receipt", ""]
    if summary.get("refused"):
        ref = summary["refused"]
        lines += [
            f"**REFUSED — {ref['reason']}.** {ref['detail']}",
            "",
            f"Exit code: {summary['exit_code']}",
            "",
        ]
    commits = sorted({r["source"]["repo_commit"] for r in receipts})
    lines += [
        f"- **Tool:** {describe(tool)}",
        f"- **Repository commit:** {', '.join(c[:12] for c in commits) or '—'}",
        f"- **Receipts:** {len(receipts)} · **exit code:** {summary['exit_code']}",
        "",
        "## Summary",
        "",
        _md_code("\n".join(i["pr_line"] for i in summary["invariants"]) or "(no receipts)"),
        "",
    ]
    for inv in summary["invariants"]:
        rs = sorted(by[inv["invariant"]], key=lambda r: r["mutation"]["id"])
        first = rs[0]
        runner = first["runner"]
        lines += [
            f"## {inv['invariant']} — {inv['status']}",
            "",
        ]
        if first.get("description"):
            lines += [first["description"], ""]
        lines += [
            f"- **Named test:** `{first['test']}`",
            f"- **Runner:** {runner['kind'] or '—'} · parser `{runner['parser'] or '—'}` · {runner.get('version') or ''}",
            f"- **Project:** `{first['source']['project']}` at `{first['source']['repo_commit'][:12]}`",
            f"- **Baseline:** {_baseline_line(first)}",
            f"- **Source tree:** {_tree_line(first)}",
            "",
            "| Mutation | Verdict | Reason | Named test | Oracle | Failure signature |",
            "|---|---|---|---|---|---|",
        ]
        for r in rs:
            m = r["mutation"]
            run = m["run"] or {}
            oracle = {True: "matched", False: "NOT matched", None: "—"}[run.get("oracle_matched")]
            lines.append(
                f"| `{m['id']}` | **{r['verdict']}** | `{r['reason']}` | {run.get('named_test_result', '—')} "
                f"| {oracle} | `{_short(run.get('failure_signature'))}` |"
            )
        lines.append("")
        for r in rs:
            m = r["mutation"]
            run = m["run"]
            lines += [
                f"### `{m['id']}` — {r['verdict']} ({r['reason']})",
                "",
                r["reason_detail"],
                "",
            ]
            if m.get("description"):
                lines += [f"*{m['description']}*", ""]
            lines += [f"- **Edit:** {e}" for e in _edit_lines(m)]
            lines += [
                f"- **Applied:** {m['applied']} · **changed:** {m['changed']}"
                + (f" — {m['apply_detail']}" if m["apply_detail"] else ""),
                f"- **Mutation fingerprint:** `{_short(m['fingerprint'], 16)}`",
                f"- **Build:** {m['compile']['status']}",
            ]
            matches = r["expect"]["matches"]
            lines.append(
                "- **Oracle:** named test must fail"
                + (f" with output matching `{matches}`" if matches else "")
            )
            if run:
                lines += [
                    f"- **Run:** exit {run['exit']} · discovered {run['discovered']} · passed {run['passed']} "
                    f"· failed {run['failed']} · {run['seconds']}s · parser `{run['parser']}`",
                    f"- **Command:** `{' '.join(run['command'])}`",
                    f"- **Named test:** {run['named_test_result']} (matches: {run.get('named_test_matches')})",
                ]
                if run.get("failure_signature"):
                    lines += [
                        f"- **Failure signature:** `{_short(run['failure_signature'], 16)}`, hashed from:",
                        "",
                        _md_code(run["failure_signature_basis"] or ""),
                    ]
                if run.get("failure_excerpt"):
                    lines += [
                        "",
                        "Named test's own failure output:",
                        "",
                        _md_code(run["failure_excerpt"]),
                    ]
            if m.get("diff"):
                lines += ["", "The exact edit:", "", _md_code(m["diff"], "diff")]
            lines.append("")
    lines += ["---", "", f"Generated from the receipt JSON by {describe(tool)}.", ""]
    return "\n".join(lines)


# -------------------------------------------------------------------- html --

CSS = """
:root { --bg:#fbfaf7; --fg:#1c1c1c; --muted:#5a5a5a; --card:#ffffff; --line:#d9d6cf;
  --caught:#1b7a3a; --survived:#b3261e; --invalid:#8a5a00; --unknown:#555c66;
  --two:#1b7a3a; --one:#2e5fd1; --gap:#b3261e; --inc:#8a5a00; --code:#f1efe9; }
@media (prefers-color-scheme: dark) { :root { --bg:#16171a; --fg:#ececec; --muted:#a9a9a9;
  --card:#1f2126; --line:#3a3d44; --caught:#4cc47a; --survived:#ff7b72; --invalid:#e0a640;
  --unknown:#9aa3ad; --two:#4cc47a; --one:#7aa2ff; --gap:#ff7b72; --inc:#e0a640; --code:#26282e; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:18px/1.5 system-ui, -apple-system,
  "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 2rem; margin: 0 0 8px; } h2 { font-size: 1.5rem; margin: 40px 0 8px; }
h3 { font-size: 1.15rem; margin: 24px 0 6px; }
.meta { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-left: 8px solid var(--line);
  border-radius: 8px; padding: 12px 16px; margin: 12px 0; }
.status { font-weight: 800; letter-spacing: .5px; }
.s-GAP { border-left-color: var(--gap); } .s-GAP .status { color: var(--gap); }
.s-TWO { border-left-color: var(--two); } .s-TWO .status { color: var(--two); }
.s-ONE { border-left-color: var(--one); } .s-ONE .status { color: var(--one); }
.s-INC { border-left-color: var(--inc); } .s-INC .status { color: var(--inc); }
.v { font-weight: 800; } .v-CAUGHT { color: var(--caught); } .v-SURVIVED { color: var(--survived); }
.v-INVALID { color: var(--invalid); } .v-UNKNOWN { color: var(--unknown); }
table { border-collapse: collapse; width: 100%; font-size: .95rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
.scroll { overflow-x: auto; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: .9em; }
pre { background: var(--code); padding: 10px 12px; border-radius: 6px; overflow-x: auto;
  white-space: pre-wrap; word-break: break-word; }
details { margin: 6px 0; } summary { cursor: pointer; color: var(--muted); }
footer { margin-top: 48px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted);
  font-size: .9rem; }
.refused { border-left-color: var(--survived); }
"""

_STATUS_CLASS = {
    summ.GAP_FOUND: "GAP",
    summ.TWO_WAY: "TWO",
    summ.ONE_WAY: "ONE",
    summ.INCOMPLETE: "INC",
}


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def html_page(summary: dict, receipts: list[dict], render_tool: dict) -> str:
    by = {}
    for r in receipts:
        by.setdefault(r["invariant"], []).append(r)
    tool = summary["tool"]
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Invariant receipt</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>Invariant receipt</h1>",
        f'<p class="meta">{len(receipts)} receipt(s) · exit code {summary["exit_code"]}</p>',
    ]
    if summary.get("refused"):
        ref = summary["refused"]
        parts.append(
            f'<div class="card refused"><span class="status v-SURVIVED">REFUSED — {_e(ref["reason"])}</span>'
            f"<p>{_e(ref['detail'])}</p></div>"
        )
    for inv in summary["invariants"]:
        cls = _STATUS_CLASS[inv["status"]]
        parts.append(
            f'<div class="card s-{cls}"><span class="status">{_e(inv["status"])}</span> '
            f'<code>{_e(inv["invariant"])}</code><div class="meta">{_e(inv["pr_line"])}</div></div>'
        )
    for inv in summary["invariants"]:
        rs = sorted(by[inv["invariant"]], key=lambda r: r["mutation"]["id"])
        first = rs[0]
        runner = first["runner"]
        cls = _STATUS_CLASS[inv["status"]]
        parts += [
            f"<h2>{_e(inv['invariant'])}</h2>",
            f'<div class="card s-{cls}"><span class="status">{_e(inv["status"])}</span>',
            f"<p>{_e(first.get('description') or '')}</p>",
            "<ul>",
            f"<li><b>Named test:</b> <code>{_e(first['test'])}</code></li>",
            f"<li><b>Runner:</b> {_e(runner['kind'])} · parser <code>{_e(runner['parser'])}</code> · {_e(runner.get('version'))}</li>",
            f"<li><b>Commit:</b> <code>{_e(first['source']['repo_commit'][:12])}</code> · project <code>{_e(first['source']['project'])}</code></li>",
            f"<li><b>Baseline:</b> {_e(_baseline_line(first))}</li>",
            f"<li><b>Source tree:</b> {_e(_tree_line(first))}</li>",
            "</ul></div>",
            '<div class="scroll"><table><thead><tr><th>Mutation</th><th>Verdict</th><th>Reason</th>'
            "<th>Named test</th><th>Oracle</th><th>Signature</th></tr></thead><tbody>",
        ]
        for r in rs:
            m, run = r["mutation"], r["mutation"]["run"] or {}
            oracle = {True: "matched", False: "NOT matched", None: "—"}[run.get("oracle_matched")]
            parts.append(
                f'<tr><td><code>{_e(m["id"])}</code></td><td class="v v-{r["verdict"]}">{r["verdict"]}</td>'
                f"<td><code>{_e(r['reason'])}</code></td><td>{_e(run.get('named_test_result', '—'))}</td>"
                f"<td>{oracle}</td><td><code>{_e(_short(run.get('failure_signature')))}</code></td></tr>"
            )
        parts.append("</tbody></table></div>")
        for r in rs:
            m, run = r["mutation"], r["mutation"]["run"]
            matches = r["expect"]["matches"]
            parts += [
                f'<h3><code>{_e(m["id"])}</code> — <span class="v v-{r["verdict"]}">{r["verdict"]}</span> '
                f"(<code>{_e(r['reason'])}</code>)</h3>",
                f"<p>{_e(r['reason_detail'])}</p>",
                "<ul>",
                *[f"<li><b>Edit:</b> {_e(e)}</li>" for e in _edit_lines(m)],
                f"<li><b>Applied:</b> {_e(m['applied'])} · <b>changed:</b> {_e(m['changed'])} {_e(m['apply_detail'])}</li>",
                f"<li><b>Fingerprint:</b> <code>{_e(_short(m['fingerprint'], 16))}</code> · <b>build:</b> {_e(m['compile']['status'])}</li>",
                f"<li><b>Oracle:</b> named test must fail{(' with output matching <code>' + _e(matches) + '</code>') if matches else ''}</li>",
            ]
            if run:
                parts += [
                    f"<li><b>Run:</b> exit {_e(run['exit'])} · discovered {_e(run['discovered'])} · passed {run['passed']} "
                    f"· failed {run['failed']} · {run['seconds']}s · parser <code>{_e(run['parser'])}</code></li>",
                    f"<li><b>Command:</b> <code>{_e(' '.join(run['command']))}</code></li>",
                    f"<li><b>Named test:</b> {_e(run['named_test_result'])}</li>",
                ]
            parts.append("</ul>")
            if run and run.get("failure_signature_basis"):
                parts.append(
                    f"<details><summary>Failure signature <code>{_e(_short(run['failure_signature'], 16))}</code> — hashed from</summary>"
                    f"<pre>{_e(run['failure_signature_basis'])}</pre></details>"
                )
            if run and run.get("failure_excerpt"):
                parts.append(
                    f"<details><summary>Named test's own failure output</summary><pre>{_e(run['failure_excerpt'])}</pre></details>"
                )
            if m.get("diff"):
                parts.append(
                    f"<details><summary>The exact edit</summary><pre>{_e(m['diff'])}</pre></details>"
                )
    footer = f"Receipts produced by {describe(tool)}."
    if describe(render_tool) != describe(tool):
        footer += f" Rendered by {describe(render_tool)}."
    parts += [f'<footer id="build-identity">{_e(footer)}</footer>', "</main></body></html>"]
    return "\n".join(parts)


def refused_summary(tool: dict, reason: str, detail: str) -> dict:
    return {
        "schema": "invariant-receipt/summary/1",
        "tool": tool,
        "exit_code": summ.EXIT_REFUSED,
        "refused": {"reason": reason, "detail": detail},
        "invariants": [],
    }


def clear_views(out: Path) -> None:
    for name in VIEWS:
        p = out / name
        if p.exists():
            p.unlink()


def write_views(out: Path, summary: dict, receipts: list[dict], render_tool: dict) -> None:
    validate_summary(summary)
    write_json(out / "summary.json", summary)
    (out / "receipt.md").write_text(markdown(summary, receipts), encoding="utf-8")
    (out / "receipt.html").write_text(html_page(summary, receipts, render_tool), encoding="utf-8")
    if summary.get("refused"):
        pr = f"REFUSED — {summary['refused']['reason']}: {summary['refused']['detail']}"
    else:
        pr = "\n".join(i["pr_line"] for i in summary["invariants"])
    (out / "pr-summary.txt").write_text(pr + "\n", encoding="utf-8")


def render(out: Path, render_tool: dict) -> dict:
    """Rebuild every view from the receipts in ``out``. Returns the summary."""
    out = Path(out)
    if not out.is_dir():
        raise RenderError(f"{out} is not a directory")
    previous = None
    if (out / "summary.json").is_file():
        try:
            previous = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    clear_views(out)  # a failed render must not leave stale views behind
    receipts = load_receipts(out)
    if not receipts and previous and previous.get("refused"):
        summary = previous
    else:
        tool = receipts[0]["tool"] if receipts else render_tool
        summary = summ.build(receipts, tool)
    write_views(out, summary, receipts, render_tool)
    return summary
