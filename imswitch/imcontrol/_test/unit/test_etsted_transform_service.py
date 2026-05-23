from dataclasses import dataclass
import json

import numpy as np
import pytest

from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService


@dataclass
class _EtSTEDInfo:
    swapXY: bool = False
    invertX: bool = False
    invertY: bool = False


def _identity_coefficients() -> np.ndarray:
    coefficients = np.zeros(20)
    coefficients[7] = 1
    coefficients[18] = 1
    return coefficients


def test_transform_service_loads_coefficients_and_applies_module_transform(tmp_path):
    (tmp_path / 'identity_transform.py').write_text(
        """
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService

def identity_transform(coords, coefficients):
    return EtSTEDTransformService.poly_thirdorder_transform(coefficients, coords)
"""
    )
    np.savetxt(tmp_path / 'identity_coefficients.csv', _identity_coefficients())

    service = EtSTEDTransformService()
    service.load(str(tmp_path), 'identity_transform', 'identity_coefficients')

    np.testing.assert_allclose(service.apply([3.0, 4.0]), [3.0, 4.0])


def test_transform_service_validates_coefficient_count(tmp_path):
    np.savetxt(tmp_path / 'short.csv', np.zeros(19))

    with pytest.raises(ValueError, match='20 coefficients'):
        EtSTEDTransformService.load_coefficients(tmp_path / 'short.csv')


def test_transform_service_applies_setup_coordinate_flags():
    service = EtSTEDTransformService()
    coords = service.prepare_input_coords([2.0, 5.0], _EtSTEDInfo(swapXY=True, invertX=True))

    np.testing.assert_allclose(coords, [-5.0, 2.0])


def test_transform_service_calibrates_identity_transform():
    low_res = np.array([
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 1.0],
        [2.0, 0.0],
        [0.0, 2.0],
        [2.0, 2.0],
        [3.0, 1.0],
        [1.0, 3.0],
        [3.0, 3.0],
    ])
    service = EtSTEDTransformService()

    coefficients = service.calibrate(low_res, low_res)
    transformed = np.array([
        service.poly_thirdorder_transform(coefficients, coord)
        for coord in low_res
    ])

    np.testing.assert_allclose(transformed, low_res, atol=1e-5)


def test_transform_service_saves_calibration_metadata(tmp_path):
    service = EtSTEDTransformService()
    coefficients = _identity_coefficients()

    service.save_calibration_metadata(
        tmp_path / 'metadata.json',
        [[0, 0], [1, 1]],
        [[0, 0], [1, 1]],
        coefficients,
    )

    metadata = json.loads((tmp_path / 'metadata.json').read_text())
    assert metadata['model'] == 'third_order_polynomial'
    assert metadata['coefficient_count'] == 20
    assert metadata['low_res_coords'] == [[0.0, 0.0], [1.0, 1.0]]
    assert metadata['high_res_coords'] == [[0.0, 0.0], [1.0, 1.0]]
    assert metadata['coefficients'] == coefficients.tolist()
