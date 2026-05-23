from collections import deque

import numpy as np
import pytest

from imswitch.imcontrol.model.EtSTEDPipelineRunner import (
    EtSTEDPipelineRunner,
)


class _ParamEdit:
    def __init__(self, value: str) -> None:
        self._value = value

    def text(self) -> str:
        return self._value


def _write_pipeline(tmp_path, name: str, source: str) -> None:
    (tmp_path / f'{name}.py').write_text(source)


def test_pipeline_runner_loads_parses_and_executes_experiment_mode(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        'valid_pipeline',
        """
import numpy as np

def valid_pipeline(img, prev_frames, binary_mask, testmode, exinfo, threshold=2.5):
    return np.array([[threshold, 4.0]]), {"seen": len(prev_frames)}
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    runner = EtSTEDPipelineRunner()
    parameters = runner.load('valid_pipeline')
    values = runner.parse_parameter_values([_ParamEdit('3.25')])
    result = runner.execute(
        np.zeros((4, 4)),
        deque([np.ones((4, 4))]),
        None,
        False,
        None,
        values,
    )

    assert 'threshold' in parameters
    assert values == [3.25]
    np.testing.assert_allclose(result.coords_detected, [[3.25, 4.0]])
    assert result.exinfo == {'seen': 1}
    assert result.analysis_image is None


def test_pipeline_runner_validates_signature(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        'bad_pipeline',
        """
def bad_pipeline(img):
    return None
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    runner = EtSTEDPipelineRunner()
    with pytest.raises(RuntimeError, match='missing required parameters'):
        runner.load('bad_pipeline')


def test_pipeline_runner_validates_test_mode_result_shape(tmp_path, monkeypatch):
    _write_pipeline(
        tmp_path,
        'wrong_result_pipeline',
        """
def wrong_result_pipeline(img, prev_frames, binary_mask, testmode, exinfo):
    return [], exinfo
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    runner = EtSTEDPipelineRunner()
    runner.load('wrong_result_pipeline')

    with pytest.raises(RuntimeError, match='analysis_image'):
        runner.execute(
            np.zeros((4, 4)),
            deque(),
            None,
            True,
            None,
            [],
        )
