"""Build a setup's pulse generators (docs/design/plans/
scan-engines-and-expert-scan.md §5.3).

Every ``pulseGenerators`` entry is resolved through the device-plugin
registry (kind ``pulse_generator``), so a generator joins by its plugin and a
setup may have several. Each is built as ``cls(info, name,
**lowLevelManagers)``. The setup's ``teensyPulse`` section still builds the
one generator it always did, ``pulseGeneratorManager``.

A consumer names the generator it drives (a scan engine's or a laser's
``managerProperties.pulseGenerator``); ``pulse_generator_named`` is the one
rule for finding it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from imswitch.imcontrol.model.plugins.registry import get_default_registry
from imswitch.imcontrol.model.scan_program.engine import SetupError

#: The generator the ``teensyPulse`` section builds, as the low-level
#: managers name it -- what a consumer drives when it names none.
LEGACY_GENERATOR = 'pulseGeneratorManager'


@dataclass
class PulseGenerators:
    generators: Dict[str, object] = field(default_factory=dict)

    def finalize(self):
        """Close every generator; ``False`` when one did not close."""
        complete = True
        for generator in self.generators.values():
            finalize = getattr(generator, 'finalize', None)
            if callable(finalize) and finalize() is False:
                complete = False
        return complete


def build_pulse_generators(setupInfo, *, lowLevelManagers: Mapping = None,
                           registry=None) -> PulseGenerators:
    declared = getattr(setupInfo, 'pulseGenerators', None)
    declared = dict(declared) if isinstance(declared, Mapping) else {}
    if LEGACY_GENERATOR in declared:
        raise SetupError(f'pulseGenerators names a generator {LEGACY_GENERATOR!r}: that name '
                         'is the teensyPulse section\'s. Name it something else.')
    registry = registry or get_default_registry()
    lowLevel = dict(lowLevelManagers or {})
    generators = {}
    for name, info in declared.items():
        cls = registry.load_manager_class('pulse_generator', info.managerName)
        if cls is None:
            raise SetupError(registry.format_resolution_error('pulse_generator',
                                                              info.managerName))
        generators[name] = cls(info, name, **lowLevel)
    return PulseGenerators(generators)


def pulse_generator_named(lowLevelManagers: Mapping, name: Optional[str]):
    """``(generator, available)``: the generator ``name`` names -- a
    ``pulseGenerators`` entry, or the ``teensyPulse`` section's when the name
    is ``None`` or ``LEGACY_GENERATOR`` -- or ``None``, with the names the
    setup has for the error."""
    built = lowLevelManagers.get('pulseGenerators')
    generators = dict(built.generators) if isinstance(built, PulseGenerators) else {}
    legacy = lowLevelManagers.get(LEGACY_GENERATOR)
    available = sorted(generators) + ([LEGACY_GENERATOR] if legacy is not None else [])
    if name is None or name == LEGACY_GENERATOR:
        return legacy, available
    return generators.get(name), available


__all__ = ['LEGACY_GENERATOR', 'PulseGenerators', 'build_pulse_generators',
           'pulse_generator_named']


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
