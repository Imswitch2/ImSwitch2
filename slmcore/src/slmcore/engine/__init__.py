"""Central state, parameter, registry, and runtime machinery for slmcore.

This package intentionally stays lightweight at import time. Public symbols are
re-exported from :mod:`slmcore`; implementation modules live here so the
architectural backbone is easy to find without eagerly importing feature
implementations.
"""
