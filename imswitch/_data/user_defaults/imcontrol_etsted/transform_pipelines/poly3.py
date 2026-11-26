"""etSTED coordinate transform: third-order polynomial, 20 coefficients.

Input ``coords`` is ``(c1, c2) = (column, row)`` of the camera frame (what the
analysis pipeline returns); the output is the centres, in um, of the first and
second active scan axis. The coefficient file lists a0..a19, one per line.
"""
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService


def poly3(coords, coefficients):
    return EtSTEDTransformService.poly_thirdorder_transform(coefficients, coords)
