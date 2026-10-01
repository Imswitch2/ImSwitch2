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
    CAMERA_PIXEL_SIZE_PARAM, DetectorManager, configuredCameraPixelSize,
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


def test_omitted_key_uses_default_and_says_so(monkeypatch):
    """Leaving the property out is allowed, but the placeholder that results
    lands in every file's PhysicalSize looking like a calibration -- so it is
    named once at startup rather than passed off silently."""
    messages = _warnings(monkeypatch)
    param = DetectorManager.makeCameraPixelSizeParameter(_info({'cameraListIndex': 0}))
    assert param.value == pytest.approx(0.15)
    assert len(messages) == 1
    assert 'not declared' in messages[0] and '0.15' in messages[0]


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


# --- ownership: who is allowed to change the value at runtime -------------

class _Camera(DetectorManager):
    """Minimal concrete manager, only to exercise the base-class bookkeeping."""

    def __init__(self, detectorInfo, withPixelSizeParam=True):
        parameters = {}
        if withPixelSizeParam:
            parameters[CAMERA_PIXEL_SIZE_PARAM] = \
                DetectorManager.makeCameraPixelSizeParameter(detectorInfo)
        super().__init__(detectorInfo, 'Cam', fullShape=(64, 64),
                         supportedBinnings=[1], model='Mock',
                         parameters=parameters)

    def crop(self, hpos, vpos, hsize, vsize): pass
    def getLatestFrame(self): return None
    def getChunk(self): return None
    def flushBuffers(self): pass
    def startAcquisition(self): pass
    def stopAcquisition(self): pass


@pytest.mark.parametrize('managerProperties, configured', [
    ({'cameraPixelSizeUm': 0.082}, True),
    ({'cameraPixelSizeUm': '0.082'}, True),
    ({'cameraListIndex': 0}, False),
    # A misspelled or unparseable value means the running value is the 0.15
    # default, not a calibration -- so it stays the user's to set and persist.
    ({'camerapixelsizeum': 0.082}, False),
    ({'cameraPixelSizeUm': '0,082'}, False),
])
def test_config_ownership_follows_a_usable_configured_value(
        monkeypatch, managerProperties, configured):
    _warnings(monkeypatch)
    camera = _Camera(_info(managerProperties))

    expected = {CAMERA_PIXEL_SIZE_PARAM} if configured else set()
    assert set(camera.configOwnedParameters) == expected


def test_no_pixel_size_parameter_means_nothing_is_config_owned(monkeypatch):
    """APD/PMT-style managers derive the pixel size from the scan instead."""
    _warnings(monkeypatch)
    camera = _Camera(_info({'cameraPixelSizeUm': 0.082}), withPixelSizeParam=False)

    assert set(camera.configOwnedParameters) == set()


@pytest.mark.parametrize('raw, expected', [
    (0.082, 0.082),
    ('0.082', 0.082),
    (None, None),
    ('0,082', None),
    ('', None),
])
def test_configured_camera_pixel_size_parses_or_returns_none(raw, expected):
    properties = {} if raw is None else {'cameraPixelSizeUm': raw}
    result = configuredCameraPixelSize(properties)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Provenance: a file can tell a measured calibration from the placeholder
# ---------------------------------------------------------------------------


def _hamamatsu_manager(props):
    from dataclasses import replace

    from imswitch.imcontrol.model import DetectorsManager
    from . import detectorInfosBasic

    name, info = next(iter(detectorInfosBasic.items()))
    info = replace(info, managerProperties={**info.managerProperties, **props})
    return DetectorsManager({name: info}, updatePeriod=100)[name]


def test_an_assumed_pixel_size_is_marked_as_such_next_to_the_value():
    manager = _hamamatsu_manager({})
    assert manager.parameters['Camera pixel size'].value == pytest.approx(0.15)
    assert manager.parameters['Camera pixel size source'].value == 'assumed default'


def test_a_declared_pixel_size_is_marked_as_the_setup_files():
    manager = _hamamatsu_manager({'cameraPixelSizeUm': 0.082})
    assert manager.parameters['Camera pixel size'].value == pytest.approx(0.082)
    assert manager.parameters['Camera pixel size source'].value == 'setup file'


def test_editing_the_pixel_size_marks_it_as_the_users():
    manager = _hamamatsu_manager({})
    manager.setParameter('Camera pixel size', 0.15)  # unchanged: still the default
    assert manager.parameters['Camera pixel size source'].value == 'assumed default'
    manager.setParameter('Camera pixel size', 0.2)
    assert manager.parameters['Camera pixel size source'].value == 'user'
