"""Failure signatures and the invariant summary (spec §4)."""

from conftest import DATA

from invariant_receipt import signature, summary
from invariant_receipt.observation import RunObservation
from invariant_receipt.runners import cargo_text, junit

NAMED = "tests::rejects_duplicate_id"


def test_signature_is_the_location_not_the_message():
    """Tom, 2026-09-28: same assertion location = same way, whatever values it prints."""
    a = "thread 't' (805836) panicked at src/lib.rs:90:9:\nassertion `left == right` failed\n  left: 2\n right: 1"
    b = "thread 't' (123) panicked at src/lib.rs:90:9:\nassertion `left == right` failed\n  left: 3\n right: 1"
    ka, ba = signature.basis(NAMED, a)
    kb, bb = signature.basis(NAMED, b)
    assert ka == kb == signature.LOCATION
    assert ba == bb == f"{NAMED}\n@ src/lib.rs:90"


def test_different_assertion_location_is_a_different_signature():
    a = "thread 't' panicked at src/lib.rs:88:14:\nsecond registration must be rejected: ()"
    b = "thread 't' panicked at src/lib.rs:90:9:\nregistry must hold one entry"
    assert signature.signature(signature.basis(NAMED, a)[1]) != signature.signature(
        signature.basis(NAMED, b)[1]
    )


def test_the_named_test_is_part_of_the_signature():
    t = "thread 't' panicked at src/lib.rs:90:9:\nx"
    assert signature.basis("tests::a", t)[1] != signature.basis("tests::b", t)[1]


def test_location_inside_the_copy_is_made_relative():
    t = "/tmp/invariant-receipt-abc/inv--m1/src/irfix/ids.py:11: ValueError"
    kind, b = signature.basis(
        "t.py::x", t, "ValueError: duplicate id 7", root="/tmp/invariant-receipt-abc/inv--m1"
    )
    assert (kind, b) == (signature.LOCATION, "t.py::x\n@ src/irfix/ids.py:11")


def test_no_location_falls_back_to_the_message_with_values_stripped():
    a = "Error: duplicate id 7 accepted at 0x7f00aa for 'alice' after 3 tries"
    b = "Error: duplicate id 12 accepted at 0x55bb for 'bob' after 9 tries"
    ka, ba = signature.basis(NAMED, a)
    assert ka == signature.MESSAGE
    assert ba == signature.basis(NAMED, b)[1]
    assert not any(ch.isdigit() for ch in ba.split("\n", 1)[1])
    assert "alice" not in ba


DIFF_ABOVE = """diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -20,3 +20,5 @@ impl Registry {
         if self.seen.contains(&id) {
+            self.seen.push(id);
+            self.seen.push(id);
             return Err(format!("duplicate id {id}"));
"""


def test_location_is_mapped_back_to_the_unmutated_line_numbers():
    """A mutation that inserts lines above the test shifts its assertion down;
    the same assertion must still be the same place."""
    shifted = "thread 't' panicked at src/lib.rs:92:9:\nleft: 3"
    unshifted = "thread 't' panicked at src/lib.rs:90:9:\nleft: 2"
    assert signature.basis(NAMED, shifted, diff=DIFF_ABOVE)[1] == f"{NAMED}\n@ src/lib.rs:90"
    assert signature.basis(NAMED, unshifted)[1] == f"{NAMED}\n@ src/lib.rs:90"


def test_a_failure_inside_the_inserted_lines_is_named_as_such():
    t = "thread 't' panicked at src/lib.rs:21:13:\nboom"
    assert signature.basis(NAMED, t, diff=DIFF_ABOVE)[1] == (
        f"{NAMED}\n@ src/lib.rs:21 (inside lines the mutation inserted)"
    )


def test_lines_above_the_change_and_other_files_are_untouched():
    assert signature.basis(NAMED, "panicked at src/lib.rs:5:1:\nx", diff=DIFF_ABOVE)[1].endswith(
        "src/lib.rs:5"
    )
    assert signature.basis(NAMED, "panicked at tests/other.rs:92:1:\nx", diff=DIFF_ABOVE)[
        1
    ].endswith("tests/other.rs:92")


def test_nothing_to_hash_gives_no_signature():
    assert signature.basis(NAMED, "", None) == (None, None)


def test_nextest_and_cargo_text_give_the_same_signature_for_the_same_failure():
    t = RunObservation(parser="cargo-text", exit=101)
    cargo_text.parse((DATA / "cargo_text" / "fail_single.txt").read_text(), t)
    n = RunObservation(parser="nextest-junit", exit=100)
    junit.parse_nextest(DATA / "nextest" / "fail_single.xml", n)
    ct, nt = t.named(NAMED)[1], n.named(NAMED)[1]
    assert signature.basis(NAMED, ct.text, ct.message) == signature.basis(
        NAMED, nt.text, nt.message
    )
    assert signature.basis(NAMED, ct.text)[1] == f"{NAMED}\n@ src/lib.rs:88"


def test_pytest_basis_is_the_crash_location():
    o = RunObservation(parser="junit-xml", exit=1)
    node = "tests/test_ids.py::test_rejects_duplicate_id"
    junit.parse_pytest(DATA / "pytest" / "fail_did_not_raise.xml", o, node)
    case = o.named(node)[1]
    assert signature.basis(node, case.text, case.message) == (
        signature.LOCATION,
        f"{node}\n@ tests/test_ids.py:14",
    )


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
