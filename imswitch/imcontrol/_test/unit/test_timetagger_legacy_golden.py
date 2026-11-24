"""The shipped FLIM mock setup drives the card exactly as it did before the
multi-detector change.

``timetagger_legacy_golden.json`` was recorded (``_timetagger_golden.py``)
on the code before the ``inputs`` block, the scan session and counting
detectors existed: every conditioning call and every measurement built
(with its arguments), in order, over two scans (the second a repeat that
reuses the ``Flim``), and a digest of each final frame's intensity,
lifetime and recorded raw image -- forward, reverse (the conditional
filter) and with a non-zero ``t0_ps``. A legacy setup must not be able to
tell the difference.
"""

from __future__ import annotations

import json

import pytest

from imswitch.imcontrol._test.unit import _timetagger_golden as golden

pytestmark = pytest.mark.nohardware

VARIANTS = {
    "forward": dict(reverse=False),
    "reverse": dict(reverse=True),
    "forward_t0": dict(reverse=False, t0_ps=700),
}


@pytest.fixture(scope="module")
def expected():
    return json.loads(golden.GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_shipped_flim_setup_matches_the_recorded_card_calls_and_products(expected, variant):
    actual = json.loads(json.dumps(golden.record(**VARIANTS[variant]), sort_keys=True))
    want = expected[variant]
    assert actual["calls"] == want["calls"]
    assert actual["scans"] == want["scans"]
    assert actual["failures"] == want["failures"] == []
    assert actual["metadata_roles"] == want["metadata_roles"]
