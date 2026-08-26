"""Numerical registration of finite 2D lattices to localized spots.

The package is intentionally split by numerical responsibility.  Public code
uses :func:`register_lattice`; the sibling modules are implementation stages of
that pipeline rather than separate user-facing APIs.
"""

from .pipeline import register_lattice

__all__ = ["register_lattice"]
