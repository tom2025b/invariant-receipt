import os
from pathlib import Path

import pytest

from irfix.flaky import should_fail
from irfix.ids import Registry, clamp_percent


def test_rejects_duplicate_id():
    """The good test: checks both halves of the invariant."""
    registry = Registry()
    registry.register(7)
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(7)
    assert len(registry) == 1, "registry must hold one entry"


def test_accepts_new_id():
    """The weak test: never registers twice, so it cannot see the rule break."""
    registry = Registry()
    registry.register(7)


@pytest.mark.parametrize("value, expected", [(-5, 0), (250, 100)])
def test_clamps(value, expected):
    assert clamp_percent(value) == expected


def test_flaky_on_alternate_runs():
    assert not should_fail(), "flaky test failed this time"


def test_escapes_the_sandbox():
    """Writes into the ORIGINAL checkout when told where it is — the kind of
    test the source-tree check exists to catch."""
    target = os.environ.get("IR_FIXTURE_TOUCH")
    if target:
        Path(target).write_text("written by a test that escaped its copy\n")
