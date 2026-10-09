"""COMET drift correction: LocalizationResult -> drift-corrected result.

Runs the optional ``comet-smlm`` package (all-pairs, GPU-optional drift
estimation); the result type is the one ``smlm-drift`` produces.
"""

from .processor import INSTALL_HINT, SmlmCometDriftProcessor, comet_installed

__all__ = ["INSTALL_HINT", "SmlmCometDriftProcessor", "comet_installed"]
