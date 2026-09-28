"""End to end on the Python fixture: the real tool, real pytest, real clones."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest
from conftest import make_repo, receipts, run_tool, validate_strict

from invariant_receipt import cli, engine
from invariant_receipt.identity import build_identity, describe

pytestmark = pytest.mark.e2e

MAIN = [
    "dup-ids-two-way",
    "dup-ids-one-way",
    "weak-test-gap",
    "invalid-cases",
    "patch-form",
    "filter-selects-nothing",
    "name-matches-no-single-test",
]

EXPECTED = {
    ("dup-ids-two-way", "drop-the-check"): ("CAUGHT", "caught"),
    ("dup-ids-two-way", "store-then-reject"): ("CAUGHT", "caught"),
    ("dup-ids-one-way", "drop-the-check"): ("CAUGHT", "caught"),
    ("dup-ids-one-way", "look-for-the-wrong-id"): ("CAUGHT", "caught"),
    ("weak-test-gap", "drop-the-check"): ("SURVIVED", "named_test_passed"),
    ("invalid-cases", "compile-break"): ("INVALID", "compile_failed"),
    ("invalid-cases", "wrong-oracle"): ("INVALID", "oracle_mismatch"),
    ("invalid-cases", "import-break"): ("INVALID", "collection_failed"),
    ("invalid-cases", "find-not-present"): ("INVALID", "mutation_not_applied"),
    ("invalid-cases", "find-matches-twice"): ("INVALID", "mutation_not_applied"),
    ("invalid-cases", "changes-nothing"): ("INVALID", "mutation_no_change"),
    ("invalid-cases", "rename-the-test"): ("INVALID", "zero_tests"),
    ("invalid-cases", "skip-the-test"): ("INVALID", "zero_tests"),
    ("invalid-cases", "fixture-error"): ("INVALID", "named_test_not_observed"),
    ("invalid-cases", "patch-does-not-apply"): ("INVALID", "mutation_not_applied"),
    ("patch-form", "drop-the-check-as-patch"): ("CAUGHT", "caught"),
    ("filter-selects-nothing", "drop-the-check"): ("INVALID", "baseline_no_tests"),
    ("name-matches-no-single-test", "break-clamp"): ("INVALID", "named_test_not_found"),
}

STATUSES = {
    "dup-ids-two-way": "TWO-WAY VERIFIED",
    "dup-ids-one-way": "ONE-WAY",
    "weak-test-gap": "GAP FOUND",
    "invalid-cases": "INCOMPLETE",
    "patch-form": "ONE-WAY",
    "filter-selects-nothing": "INCOMPLETE",
    "name-matches-no-single-test": "INCOMPLETE",
}


def only(*names):
    return [a for n in names for a in ("--only", n)]


@pytest.fixture(scope="module")
def main_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("py-main")
    repo = make_repo(tmp, "python-pkg")
    out = tmp / "out"
    proc = run_tool(
        ["run", "invariants.yaml", "--out", str(out), "--workdir", str(tmp), *only(*MAIN)], repo
    )
    return repo, out, proc


def test_every_python_fixture_case_gets_its_stated_verdict(main_run):
    repo, out, proc = main_run
    got = {k: (r["verdict"], r["reason"]) for k, r in receipts(out).items()}
    assert got == EXPECTED, proc.stderr[-3000:]
    summary = json.loads((out / "summary.json").read_text())
    assert {i["invariant"]: i["status"] for i in summary["invariants"]} == STATUSES
    assert proc.returncode == 1  # a SURVIVED wins over everything
    assert summary["exit_code"] == 1


def test_every_receipt_validates_against_the_schema_strictly(main_run):
    _repo, out, _ = main_run
    rs = receipts(out)
    assert len(rs) == len(EXPECTED)
    for r in rs.values():
        validate_strict(r)
        assert r["runner"]["parser"] == "junit-xml"
        assert r["source"]["tree_unchanged"] is True
        assert r["source"]["tree_hash_before"] == r["source"]["tree_hash_after"]


def test_one_way_breaks_share_a_signature_two_way_breaks_do_not(main_run):
    _repo, out, _ = main_run
    rs = receipts(out)
    sig = lambda k: rs[k]["mutation"]["run"]["failure_signature"]  # noqa: E731
    fp = lambda k: rs[k]["mutation"]["fingerprint"]  # noqa: E731
    assert sig(("dup-ids-one-way", "drop-the-check")) == sig(
        ("dup-ids-one-way", "look-for-the-wrong-id")
    )
    assert fp(("dup-ids-one-way", "drop-the-check")) != fp(
        ("dup-ids-one-way", "look-for-the-wrong-id")
    )
    assert sig(("dup-ids-two-way", "drop-the-check")) != sig(
        ("dup-ids-two-way", "store-then-reject")
    )
    # The patch and the find/replace make the identical edit: identical fingerprint.
    assert fp(("patch-form", "drop-the-check-as-patch")) == fp(
        ("dup-ids-two-way", "drop-the-check")
    )


def test_outputs_exist_and_render_from_json(main_run):
    _repo, out, proc = main_run
    for name in ("summary.json", "receipt.md", "receipt.html", "pr-summary.txt"):
        assert (out / name).is_file()
    pr = (out / "pr-summary.txt").read_text().splitlines()
    assert "dup-ids-two-way — TWO-WAY VERIFIED (2/2 caught)" in pr
    assert "weak-test-gap — GAP FOUND (0/1 caught, 1 survived)" in pr
    assert len(pr) == len(STATUSES)
    html = (out / "receipt.html").read_text()
    # Self-contained: no external scripts, stylesheets or images.
    assert not re.search(r"<(script|link|img)\b", html)
    assert "http://" not in html and "https://" not in html


def test_render_rebuilds_identical_views_from_the_receipts(main_run, tmp_path):
    _repo, out, _ = main_run
    before = {
        n: (out / n).read_text()
        for n in ("summary.json", "receipt.md", "receipt.html", "pr-summary.txt")
    }
    for n in before:
        (out / n).unlink()
    proc = run_tool(["render", str(out)], tmp_path)
    assert proc.returncode == 1
    for n, text in before.items():
        assert (out / n).read_text() == text, n


def test_the_word_proven_appears_in_no_output(main_run):
    _repo, out, proc = main_run
    texts = [proc.stdout, proc.stderr] + [
        p.read_text()
        for p in out.rglob("*")
        if p.is_file() and p.suffix in (".json", ".md", ".html", ".txt")
    ]
    for t in texts:
        assert not re.search(r"\bproven\b", t, re.I)


def test_flaky_baseline_is_not_stable_with_repeat_5(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    flaky = tmp_path / "flaky"
    flaky.mkdir()
    out = tmp_path / "out"
    proc = run_tool(
        ["run", "invariants.yaml", "--repeat", "5", "--out", str(out), *only("flaky-baseline")],
        repo,
        env={"IR_FIXTURE_FLAKY_DIR": str(flaky)},
    )
    (r,) = receipts(out).values()
    assert (r["verdict"], r["reason"]) == ("INVALID", "baseline_not_stable"), proc.stderr[-2000:]
    runs = r["baseline"]["runs"]
    assert [x["named_test"] for x in runs] == ["passed", "failed"]  # stops at the first red repeat
    assert r["mutation"]["run"] is None  # no mutation run is attempted
    assert proc.returncode == 2


def test_repeat_applies_to_the_baseline_only(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    proc = run_tool(
        ["run", "invariants.yaml", "--repeat", "3", "--out", str(out), *only("dup-ids-two-way")],
        repo,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    for r in receipts(out).values():
        assert r["baseline"]["repeat"] == 3
        assert len(r["baseline"]["runs"]) == 3
        assert isinstance(r["mutation"]["run"], dict)  # one run object, not a list
    assert sorted(p.name for p in (out / "dup-ids-two-way" / "baseline").glob("run-*.log")) == [
        "run-1.log",
        "run-2.log",
        "run-3.log",
    ]
    for mid in ("drop-the-check", "store-then-reject"):
        assert sorted(p.name for p in (out / "dup-ids-two-way" / mid).glob("*.log")) == [
            "compile.log",
            "run.log",
        ]


def test_every_mutation_caught_exits_zero(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    proc = run_tool(
        ["run", "invariants.yaml", "--out", str(tmp_path / "o"), *only("dup-ids-two-way")], repo
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "dup-ids-two-way — TWO-WAY VERIFIED (2/2 caught)"


def test_a_hanging_break_is_timeout_not_caught(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    proc = run_tool(
        ["run", "invariants.yaml", "--timeout", "5", "--out", str(out), *only("hangs")],
        repo,
        timeout=120,
    )
    (r,) = receipts(out).values()
    assert (r["verdict"], r["reason"]) == ("INVALID", "timeout"), proc.stderr[-2000:]
    assert r["mutation"]["run"]["timed_out"] is True
    assert r["mutation"]["run"]["seconds"] < 30
    assert proc.returncode == 2


def test_a_test_that_writes_to_the_original_tree_makes_everything_unknown(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--out",
            str(out),
            *only("escapes-the-sandbox", "dup-ids-two-way"),
        ],
        repo,
        env={"IR_FIXTURE_TOUCH": str(repo / "escaped.txt")},
    )
    assert (repo / "escaped.txt").exists()  # the fixture really did escape
    rs = receipts(out)
    assert len(rs) == 3
    for r in rs.values():
        assert (r["verdict"], r["reason"]) == ("UNKNOWN", "source_tree_changed")
        assert r["source"]["tree_unchanged"] is False
        assert "?? escaped.txt" in r["source"]["tree_status_after"]
        validate_strict(r)
    assert "CHANGED DURING THE RUN" in proc.stderr
    assert proc.returncode == 3


def test_missing_pytest_is_runner_missing(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    venv = tmp_path / "bare-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--python",
            str(venv / "bin" / "python"),
            "--out",
            str(out),
            *only("dup-ids-two-way"),
        ],
        repo,
    )
    rs = receipts(out)
    assert {(r["verdict"], r["reason"]) for r in rs.values()} == {("UNKNOWN", "runner_missing")}
    assert proc.returncode == 3


def test_copy_failure_is_unknown(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    proc = run_tool(
        [
            "run",
            "invariants.yaml",
            "--workdir",
            str(tmp_path / "no" / "such" / "dir"),
            "--out",
            str(out),
            *only("dup-ids-two-way"),
        ],
        repo,
    )
    assert {(r["verdict"], r["reason"]) for r in receipts(out).values()} == {
        ("UNKNOWN", "copy_failed")
    }
    assert proc.returncode == 3


def test_dirty_tree_is_refused_and_writes_a_refusal(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    (repo / "src" / "irfix" / "ids.py").write_text("# edited\n")
    out = tmp_path / "out"
    proc = run_tool(["run", "invariants.yaml", "--out", str(out)], repo)
    assert proc.returncode == 4
    summary = json.loads((out / "summary.json").read_text())
    assert summary["refused"]["reason"] == "dirty_tree"
    assert receipts(out) == {}


def test_untracked_file_is_dirty_too(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    (repo / "notes.txt").write_text("untracked\n")
    proc = run_tool(["run", "invariants.yaml", "--out", str(tmp_path / "out")], repo)
    assert proc.returncode == 4
    assert "notes.txt" in proc.stderr


def test_output_inside_the_repo_does_not_count_as_a_change(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    (repo / ".gitignore").write_text("__pycache__/\n")  # receipts/ NOT ignored
    subprocess.run(
        ["git", "-c", "user.name=f", "-c", "user.email=f@invalid", "commit", "-qam", "x"],
        cwd=repo,
        check=True,
    )
    for _ in range(2):  # second run: the first run's receipts are in the tree
        proc = run_tool(["run", "invariants.yaml", *only("dup-ids-two-way")], repo)
        assert proc.returncode == 0, proc.stderr[-2000:]


def test_stale_receipts_from_an_earlier_run_are_removed(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    run_tool(["run", "invariants.yaml", "--out", str(out), *only("dup-ids-two-way")], repo)
    assert receipts(out)
    run_tool(["run", "invariants.yaml", "--out", str(out), *only("weak-test-gap")], repo)
    assert {k[0] for k in receipts(out)} == {"weak-test-gap"}
    (repo / "dirty.txt").write_text("x")
    proc = run_tool(["run", "invariants.yaml", "--out", str(out)], repo)
    assert proc.returncode == 4
    assert receipts(out) == {}  # a refused run leaves no old verdicts behind
    assert "REFUSED" in (out / "pr-summary.txt").read_text()


def test_a_directory_the_tool_did_not_create_is_never_emptied(tmp_path):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "mine"
    out.mkdir()
    (out / "precious.txt").write_text("keep me")
    proc = run_tool(["run", "invariants.yaml", "--out", str(out)], repo)
    assert proc.returncode == 4
    assert (out / "precious.txt").read_text() == "keep me"


def test_usage_error_exits_4_not_2(tmp_path):
    proc = run_tool(["run"], tmp_path)
    assert proc.returncode == 4


def test_a_crash_in_the_tool_still_writes_unknown_receipts(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"

    def boom(*a, **k):
        raise RuntimeError("simulated bug in the tool")

    monkeypatch.setattr(engine, "apply_mutation", boom)
    code = cli.main(
        ["run", str(repo / "invariants.yaml"), "--out", str(out), *only("dup-ids-two-way")]
    )
    rs = receipts(out)
    assert len(rs) == 2
    for r in rs.values():
        assert (r["verdict"], r["reason"]) == ("UNKNOWN", "internal_error")
        assert "simulated bug" in r["reason_detail"]
        validate_strict(r)
    assert code == 3


def test_a_crash_between_invariants_marks_the_rest_unknown(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "python-pkg")
    out = tmp_path / "out"
    real = engine.run_invariant
    calls = []

    def flaky_engine(ctx, inv):
        calls.append(inv.name)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(ctx, inv)

    monkeypatch.setattr(engine, "run_invariant", flaky_engine)
    code = cli.main(
        [
            "run",
            str(repo / "invariants.yaml"),
            "--out",
            str(out),
            *only("dup-ids-two-way", "weak-test-gap"),
        ]
    )
    rs = receipts(out)
    assert {k: (r["verdict"], r["reason"]) for k, r in rs.items()} == {
        ("dup-ids-two-way", "drop-the-check"): ("CAUGHT", "caught"),
        ("dup-ids-two-way", "store-then-reject"): ("CAUGHT", "caught"),
        ("weak-test-gap", "drop-the-check"): ("UNKNOWN", "internal_error"),
    }
    assert code == 3  # the unknown hides nothing: nothing SURVIVED was observed


def test_build_identity_agrees_everywhere(main_run, tmp_path):
    _repo, out, _ = main_run
    version = run_tool(["--version"], tmp_path).stdout.strip()
    ident = build_identity()
    assert version == describe(ident)
    for r in receipts(out).values():
        assert describe(r["tool"]) == version
    footer = re.search(
        r'<footer id="build-identity">(.*?)</footer>', (out / "receipt.html").read_text()
    ).group(1)
    assert footer == f"Receipts produced by {version}."
