"""Shared, dependency-light analysis primitives.

These building blocks (ROI records, classical segmentation) are used by both
``imcontrol`` workflows and the ``improcess`` post-processing module. Living in
``imcommon`` — the shared core layer — lets both depend on the common code
instead of on each other, breaking the historical ``imcontrol`` <-> ``improcess``
cycle (see docs/design/audit-2026-06/02-layering-boundaries.md).

Keep this package free of any ``imcontrol``/``improcess`` imports, and keep
heavy optional dependencies (scipy/scikit-image) behind lazy function-local
imports so importing the package stays cheap.
"""
