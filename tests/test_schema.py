"""The schema itself refuses dishonest receipts — and the strict check bites."""

import copy
import json

import jsonschema
import pytest
from conftest import DATA, validate_strict

from invariant_receipt.receipt import validate_receipt

GOOD = json.loads((DATA / "receipt_caught.json").read_text())


def mutated(fn):
    r = copy.deepcopy(GOOD)
    fn(r)
    return r


def test_recorded_real_receipt_is_valid():
    validate_receipt(GOOD)
    validate_strict(GOOD)


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["mutation"]["run"].update(oracle_matched=False),
        lambda r: r["mutation"]["run"].update(named_test_result="passed"),
        lambda r: r.update(reason="named_test_passed"),
        lambda r: r["source"].update(tree_unchanged=False),
        lambda r: r["source"].update(tree_unchanged=None),
        lambda r: r.update(verdict="INVALID", reason="caught"),
        lambda r: r.update(verdict="UNKNOWN", reason="timeout"),
        lambda r: r.update(schema="invariant-receipt/receipt/2"),
    ],
)
def test_schema_rejects_a_catch_the_evidence_does_not_support(edit):
    with pytest.raises(jsonschema.ValidationError):
        validate_receipt(mutated(edit))


def test_strict_check_rejects_a_misspelt_field():
    bad = mutated(lambda r: r["mutation"]["run"].update(oracle_matchd=True))
    validate_receipt(bad)  # the shipped schema tolerates additions (spec §5)
    with pytest.raises(jsonschema.ValidationError):
        validate_strict(bad)
