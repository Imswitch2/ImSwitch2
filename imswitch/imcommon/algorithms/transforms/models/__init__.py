"""Concrete transform kinds.

Importing this package registers every built-in kind.  Linear and nonlinear
models use the same ``TransformModel`` contract; hosts that specifically need a
matrix (for example ImControl's current resample-free live display Apply path)
must check :meth:`TransformModel.as_matrix` rather than assuming every model is
affine.
"""

from .affine import AffineTransform
from .composed import ComposedTransform
from .identity import IdentityTransform
from .polynomial import PolynomialTransform
from .rotation import Rotation90Transform

__all__ = [
    "AffineTransform",
    "ComposedTransform",
    "IdentityTransform",
    "PolynomialTransform",
    "Rotation90Transform",
]
