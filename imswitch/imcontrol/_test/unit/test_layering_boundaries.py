"""Structural guards on cross-module layering (audit 2026-06, report 02).

The shared segmentation kernel lives in ``imcommon.algorithms`` so that
``imcontrol`` workflows and the ``improcess`` post-processing module can both
depend on the common layer instead of on each other. These tests keep that
boundary from silently regressing: a future top-level ``imcontrol`` -> improcess
import would re-form the historical cycle and this would fail.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _module_path(dotted: str) -> Path:
    return _REPO_ROOT.joinpath(*dotted.split(".")).with_suffix(".py")


def _top_level_imports(dotted_module: str) -> set[str]:
    """Return the modules imported at module top level (not inside functions)."""
    tree = ast.parse(_module_path(dotted_module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:  # module body only -> top level, excludes lazy imports
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_imcommon_algorithms_have_no_app_module_imports():
    """imcommon is the shared core; it must never import the app modules."""
    for module in (
        "imswitch.imcommon.algorithms.roi",
        "imswitch.imcommon.algorithms.segmentation",
    ):
        for imported in _top_level_imports(module):
            assert not imported.startswith("imswitch.imcontrol"), (module, imported)
            assert not imported.startswith("imswitch.improcess"), (module, imported)


def test_imcontrol_segmentation_workflow_uses_shared_kernel_not_improcess():
    """The tiling/cell-targeting workflow depends on the shared kernel in
    imcommon, not on improcess — otherwise imcontrol -> improcess re-forms."""
    imports = _top_level_imports("imswitch.imcontrol.model.workflows.segmentation")
    assert "imswitch.imcommon.algorithms.segmentation" in imports
    assert not any(name.startswith("imswitch.improcess") for name in imports)


def test_improcess_segmentation_shim_reexports_shared_kernel():
    """The improcess module keeps its public API by re-exporting the shared
    kernel, so existing improcess call sites are unaffected by the move."""
    from imswitch.imcommon.algorithms import segmentation as shared
    from imswitch.improcess.analysis import segmentation as shim

    for name in (
        "segment_image",
        "prepare_segmentation_image",
        "otsu_threshold",
        "SegmentationAnalysis",
        "SegmentationRegion",
    ):
        assert getattr(shim, name) is getattr(shared, name), name


def test_roi_record_identity_is_shared_across_layers():
    """A single ROIRecord type is shared, so records built by the segmentation
    kernel are the same type improcess ROI tooling consumes."""
    from imswitch.imcommon.algorithms.roi import ROIRecord as CommonROI
    from imswitch.improcess.analysis.roi_manager import ROIRecord as ImprocessROI

    assert CommonROI is ImprocessROI
