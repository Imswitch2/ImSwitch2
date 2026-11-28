"""Moved to :mod:`imswitch.imcommon.model.contrast`, so ImControl's viewer can
level with the same rules; this name stays for ImProcess and existing code.

The module object *is* the imcommon one (not a copy of its names), so setting
an attribute here -- a test's ``monkeypatch.setattr(contrast,
"_SAMPLE_WORKING_SET_BYTES", ...)`` -- changes what its functions read.
"""
import sys

from imswitch.imcommon.model import contrast as _contrast

sys.modules[__name__] = _contrast
