"""Compatibility name for the Leica DMI Z positioner.

New code should use :class:`LeicaDMIZPositionerManager`.  Keeping the legacy
manager name for now to avoid forcing existing setup config files to change.
"""

from .LeicaDMIZPositionerManager import LeicaDMIZPositionerManager


class LeicaDMIManager(LeicaDMIZPositionerManager):
    pass
