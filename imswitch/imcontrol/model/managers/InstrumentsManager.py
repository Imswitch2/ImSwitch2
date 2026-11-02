from .MultiManager import MultiManager


class InstrumentsManager(MultiManager):
    """ MultiManager for measurement instruments (power meters, polarimeters).

    Instruments are usually ``transient``: a missing one is a normal state,
    connected at runtime from the Hardware status window. """

    def __init__(self, instrumentInfos, **lowLevelManagers):
        super().__init__(instrumentInfos or {}, 'instruments', **lowLevelManagers)
