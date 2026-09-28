"""invariants.yaml validation (spec §2): strict, and never guesses."""

import pytest
from conftest import FIXTURES

from invariant_receipt.spec import SpecError, load

BASE = """
invariant: dup
test: tests/test_x.py::test_a
mutations:
  - id: m1
    file: src/a.py
    find: "x"
    replace: "y"
    expect: {outcome: fail}
"""


def write(tmp_path, text):
    p = tmp_path / "inv.yaml"
    p.write_text(text)
    return p


def test_fixture_files_load():
    assert len(load(FIXTURES / "python-pkg" / "invariants.yaml")) >= 9
    assert len(load(FIXTURES / "rust-crate" / "invariants.yaml")) >= 8


def test_single_invariant_form(tmp_path):
    (inv,) = load(write(tmp_path, BASE))
    assert inv.name == "dup" and inv.mutations[0].kind == "find_replace"


@pytest.mark.parametrize(
    "old, new, needle",
    [
        ("expect: {outcome: fail}", "expect: {outcome: fail, matchs: x}", "unknown key"),
        ("expect: {outcome: fail}", "expect: {outcome: pass}", "must be 'fail'"),
        ("expect: {outcome: fail}", "expect: {outcome: fail, matches: '('}", "regular expression"),
        ("    expect: {outcome: fail}\n", "", "'expect' is required"),
        ("file: src/a.py", "file: ../escape.py", "inside the project"),
        ("file: src/a.py", "file: /etc/passwd", "inside the project"),
        ("id: m1", "id: 'bad id'", "must match"),
        ("test: tests/test_x.py::test_a\n", "", "test must be a string"),
    ],
)
def test_bad_specs_are_refused(tmp_path, old, new, needle):
    text = BASE.replace(old, new)
    assert text != BASE
    with pytest.raises(SpecError, match=needle):
        load(write(tmp_path, text))


def test_both_forms_at_once_is_refused(tmp_path):
    text = BASE.replace(
        "    expect: {outcome: fail}", "    patch: p.diff\n    expect: {outcome: fail}"
    )
    (tmp_path / "p.diff").write_text("")
    with pytest.raises(SpecError, match="exactly one form"):
        load(write(tmp_path, text))


def test_empty_mutations_refused(tmp_path):
    with pytest.raises(SpecError, match="non-empty"):
        load(write(tmp_path, "invariant: a\ntest: t\nmutations: []\n"))


def test_duplicate_ids_refused(tmp_path):
    dup = (
        BASE
        + """  - id: m1
    file: src/a.py
    find: "x"
    replace: "z"
    expect: {outcome: fail}
"""
    )
    with pytest.raises(SpecError, match="duplicate mutation"):
        load(write(tmp_path, dup))
