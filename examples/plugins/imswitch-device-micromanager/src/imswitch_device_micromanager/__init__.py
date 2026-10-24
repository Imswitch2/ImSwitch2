"""Micro-Manager device bridge for ImSwitch2, built on pymmcore-plus.

The manager classes are intentionally not imported here: ImSwitch resolves them
lazily through the manifest's ``python_name`` entries, so importing this package
must not pull in numpy, the plugin API, or pymmcore-plus.
"""

__version__ = "0.1.0"
