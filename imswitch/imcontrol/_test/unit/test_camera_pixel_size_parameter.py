"""Regression tests for DetectorManager.makeCameraPixelSizeParameter.

Background: a setup file that spells the key wrong ("Camerapixelsizeum") or
gives it a decimal comma ("0,082" -- what a German-locale keyboard produces)
used to fall back to the 0.15 µm default with no diagnostic at all. From the
rig that looks like "the camera pixel size keeps resetting itself to 0.15",
with nothing in the log pointing at the setup file. Both cases must now warn.

Only plugin-provided detectors declare cameraPixelSizeUm in a JSON schema, so
the built-in managers (Hamamatsu among them) are exactly the ones where the
key gets hand-typed into the config editor -- and therefore mistyped.
"""
import pytest

from imswitch.imcontrol.model.SetupInfo import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    DetectorManager,
)


def _info(managerProperties):
    return DetectorInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='HamamatsuManager',
        managerProperties=managerProperties,
        forAcquisition=True,
        forFocusLock=False,
    )


def _warnings(monkeypatch):
    """Capture what the helper logs, without touching the real logger."""
    messages = []

    class _Logger:
        def warning(self, message):
            messages.append(message)

    monkeypatch.setattr(
        'imswitch.imcontrol.model.managers.detectors.DetectorManager.initLogger',
        lambda *args, **kwargs: _Logger(),
    )
    return messages


def test_configured_value_is_used(monkeypatch):
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(
        _info({'cameraPixelSizeUm': 0.082})
    )
    assert param.value == pytest.approx(0.082)
    assert messages == []


def test_omitted_key_uses_default_silently(monkeypatch):
    """Leaving the property out is a legitimate choice, not a mistake."""
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(_info({'cameraListIndex': 0}))
    assert param.value == pytest.approx(0.15)
    assert messages == []


@pytest.mark.parametrize(
    'typo', ['Camerapixelsizeum', 'camerapixelsizeum', 'camera_pixel_size_um']
)
def test_misspelled_key_warns_and_names_the_typo(monkeypatch, typo):
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(_info({typo: 0.082}))

    assert param.value == pytest.approx(0.15)
    assert len(messages) == 1
    assert typo in messages[0]
    assert 'cameraPixelSizeUm' in messages[0]


def test_decimal_comma_warns_instead_of_raising(monkeypatch):
    """float("0,082") raises ValueError; that used to abort manager startup."""
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(
        _info({'cameraPixelSizeUm': '0,082'})
    )

    assert param.value == pytest.approx(0.15)
    assert len(messages) == 1
    assert '0,082' in messages[0]


def test_numeric_string_is_still_accepted(monkeypatch):
    """A well-formed string stays usable -- only unparseable values warn."""
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(
        _info({'cameraPixelSizeUm': '0.082'})
    )
    assert param.value == pytest.approx(0.082)
    assert messages == []
