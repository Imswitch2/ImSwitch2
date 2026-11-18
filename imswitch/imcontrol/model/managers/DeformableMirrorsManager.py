from .MultiManager import MultiManager


class DeformableMirrorsManager(MultiManager):
    """ MultiManager for deformable mirrors (adaptive optics). """

    def __init__(self, deformableMirrorInfos, **lowLevelManagers):
        super().__init__(deformableMirrorInfos or {}, 'deformableMirrors', **lowLevelManagers)
