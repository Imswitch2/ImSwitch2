"""Loading scan parameters into the Base scan widget.

``loadScanParamsFromFile`` and setup-mode restores end in ``setParameters``.
On the Base controller that method finished by calling ``plotSignalGraph`` --
which only the MoNaLISA, Advanced and TriggerScope controllers define, since
the Base widget has no signal graph -- so every load raised ``AttributeError``
after the values had been written into the widget.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

pytest.importorskip("qtpy")

from imswitch.imcontrol.controller.controllers.ScanControllerBase import (  # noqa: E402
    ScanControllerBase,
)


def _controller(widget):
    # setParameters only reads these; a SimpleNamespace stands in for the
    # QObject, which refuses attributes until its __init__ has run.
    return SimpleNamespace(
        _widget=widget,
        TTLDevices={"Camera": object(), "Laser": object()},
        _analogParameterDict={
            "target_device": ["X", "Y"],
            "axis_length": [1.0, 2.0],
            "axis_step_size": [0.1, 0.2],
            "axis_centerpos": [0.0, 5.0],
        },
        _digitalParameterDict={
            "target_device": ["Camera"],
            "TTL_start": [[0.0]],
            "TTL_end": [[0.005]],
            "sequence_time": 0.01,
        },
        settingParameters=False,
    )


def test_set_parameters_writes_the_widget_without_raising():
    widget = mock.MagicMock()
    controller = _controller(widget)

    ScanControllerBase.setParameters(controller)

    widget.setScanSize.assert_any_call("Y", 2.0)
    widget.setScanStepSize.assert_any_call("X", 0.1)
    widget.setScanCenterPos.assert_any_call("Y", 5.0)
    widget.setTTLStarts.assert_called_once_with("Camera", [0.0])
    widget.setTTLEnds.assert_called_once_with("Camera", [0.005])
    widget.unsetTTL.assert_called_once_with("Laser")
    widget.setSeqTimePar.assert_called_once_with(0.01)
    assert controller.settingParameters is False
