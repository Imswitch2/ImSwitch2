"""The Imaging Source device support for ImSwitch2, built on IC4.

The manager classes are intentionally not imported here: ImSwitch resolves them
lazily through the manifest's ``python_name`` entries, so importing this package
must not pull in numpy, the plugin API, or the vendor SDK.
"""

__version__ = "0.1.0"
