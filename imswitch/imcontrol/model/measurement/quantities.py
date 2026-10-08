"""Quantity specs shared by real and simulated instruments of one kind, so a
mock and its real counterpart produce samples with identical names, units and
valid ranges."""
from __future__ import annotations

import math

from imswitch.imcommon.model.measurement_run import QuantitySpec

#: A power meter reading. A zeroed meter reads slightly negative in the dark,
#: so the lower bound only rejects nonsense; overrange markers (SCPI 9.9e37)
#: fall above the upper bound and become invalid samples.
POWER_QUANTITY = QuantitySpec('power', 'W', 'optical.power', -1.0, 10.0)

#: A polarimeter reading: polarisation ellipse plus degree of polarisation.
#: A sample is *invalid* only when the packet is malformed (a field missing,
#: not finite): the angles keep their geometric ranges, but DOP and power are
#: whatever the instrument computed -- in the dark a PAX1000 reports a power
#: of about zero and a meaningless, often negative DOP (seen on the rig:
#: -0.22 at -7e-11 W). That is a real reading of a dark instrument, not a
#: bad packet; whether a point qualifies is the analysis's decision (the
#: polarisation map requires DOP >= its ``dop_min`` per point).
PAX_QUANTITIES = (
    QuantitySpec('azimuth', 'rad', 'polarisation.azimuth', -math.pi / 2, math.pi / 2),
    QuantitySpec('ellipticity', 'rad', 'polarisation.ellipticity', -math.pi / 4, math.pi / 4),
    QuantitySpec('dop', '', 'polarisation.dop'),
    QuantitySpec('power', 'W', 'optical.power'),
)
