"""Concrete transform kinds.

Importing this package registers every built-in kind. Three ship at Step 0 --
identity, rotation90 and affine -- deliberately more than one, so that the
swappability the whole design rests on is exercised rather than assumed: the
same wrappers, file format and widget drive all three with no branching on
kind.
"""

from .affine import AffineTransform
from .composed import ComposedTransform
from .identity import IdentityTransform
from .rotation import Rotation90Transform

__all__ = [
    "AffineTransform",
    "ComposedTransform",
    "IdentityTransform",
    "Rotation90Transform",
]
