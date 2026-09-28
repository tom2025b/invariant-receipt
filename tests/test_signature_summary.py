"""Failure signatures and the invariant summary (spec §4)."""

from conftest import DATA

from invariant_receipt import signature, summary
from invariant_receipt.observation import RunObservation
from invariant_receipt.runners import cargo_text, junit


def test_volatile_parts_are_stripped():
    a = "thread 'tests::x' (805836) panicked at src/lib.rs:88:14:\nbad <Foo at 0x7f00aa> in /tmp/abc/def/file.rs 1.25s"
    b = "thread 'tests::x' (123) panicked at src/lib.rs:88:14:\nbad <Foo at 0x55bb> in /home/q/r/file.rs 3.5s"
    assert signature.basis(a) == signature.basis(b)


def test_different_message_same_location_is_a_different_signature():
    a = "thread 't' panicked at src/lib.rs:88:14:\nsecond registration must be rejected: ()"
    b = "thread 't' panicked at src/lib.rs:91:9:\nregistry must hold one entry"
    assert signature.signature(signature.basis(a)) != signature.signature(signature.basis(b))


def test_nextest_and_cargo_text_give_the_same_signature_for_the_same_failure():
    t = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse((DATA / "cargo_text" / "fail_single.txt").read_text(), t)
    n = RunObservation(parser="nextest-junit", exit=100)
    junit.parse_nextest(DATA / "nextest" / "fail_single.xml", n)
    name = "tests::rejects_duplicate_id"
    ct, nt = t.named(name)[1], n.named(name)[1]
    assert signature.basis(ct.text, ct.message) == signature.basis(nt.text, nt.message)


def test_pytest_basis_uses_location_and_message():
    o = RunObservation(parser="junit-xml", exit=1)
    junit.parse_pytest(
        DATA / "pytest" / "fail_did_not_raise.xml",
        o,
        "tests/test_ids.py::test_rejects_duplicate_id",
    )
    case = o.named("tests/test_ids.py::test_rejects_duplicate_id")[1]
    b = signature.basis(case.text, case.message)
    assert b.startswith("tests/test_ids.py:") and "DID NOT RAISE" in b


def r(mid, verdict, fp=None, sig=None):
    return {
        "invariant": "inv",
        "verdict": verdict,
        "reason": "x",
        "mutation": {
            "id": mid,
            "fingerprint": fp,
            "run": {"failure_signature": sig} if sig or verdict == "CAUGHT" else None,
        },
    }


def test_two_way_needs_different_fingerprints_and_signatures():
    assert (
        summary.invariant_status([r("a", "CAUGHT", "f1", "s1"), r("b", "CAUGHT", "f2", "s2")])
        == summary.TWO_WAY
    )


def test_same_signature_is_one_way():
    assert (
        summary.invariant_status([r("a", "CAUGHT", "f1", "s1"), r("b", "CAUGHT", "f2", "s1")])
        == summary.ONE_WAY
    )


def test_same_fingerprint_is_one_way():
    assert (
        summary.invariant_status([r("a", "CAUGHT", "f1", "s1"), r("b", "CAUGHT", "f1", "s2")])
        == summary.ONE_WAY
    )


def test_missing_signature_cannot_make_two_way():
    assert (
        summary.invariant_status([r("a", "CAUGHT", "f1", None), r("b", "CAUGHT", "f2", "s2")])
        == summary.ONE_WAY
    )


def test_single_catch_is_one_way():
    assert (
        summary.invariant_status([r("a", "CAUGHT", "f1", "s1"), r("b", "INVALID")])
        == summary.ONE_WAY
    )


def test_any_survivor_is_gap_found_even_with_two_way_catches():
    rs = [r("a", "CAUGHT", "f1", "s1"), r("b", "CAUGHT", "f2", "s2"), r("c", "SURVIVED")]
    assert summary.invariant_status(rs) == summary.GAP_FOUND


def test_nothing_caught_or_survived_is_incomplete():
    assert summary.invariant_status([r("a", "INVALID"), r("b", "UNKNOWN")]) == summary.INCOMPLETE


def test_exit_code_order():
    assert summary.exit_code([r("a", "CAUGHT", "f", "s")]) == 0
    assert summary.exit_code([r("a", "SURVIVED"), r("b", "UNKNOWN"), r("c", "INVALID")]) == 1
    assert summary.exit_code([r("a", "UNKNOWN"), r("c", "INVALID"), r("d", "CAUGHT")]) == 3
    assert summary.exit_code([r("c", "INVALID"), r("d", "CAUGHT")]) == 2
    assert summary.exit_code([]) == 3


def test_pr_line_format():
    rs = [r("a", "CAUGHT", "f1", "s1"), r("b", "CAUGHT", "f2", "s2")]
    assert summary.pr_line("duplicate-ids-are-rejected", summary.TWO_WAY, rs) == (
        "duplicate-ids-are-rejected — TWO-WAY VERIFIED (2/2 caught)"
    )
