"""Structural guards on cross-module layering (audit 2026-06, report 02).

The shared segmentation kernel and widget-state persistence live in
``imcommon`` so that ``imcontrol`` and the ``improcess`` post-processing module
depend on the common layer instead of on each other. These tests keep that
boundary from silently regressing: a new top-level ``imcontrol`` -> improcess
import (re-forming the historical cycle), or a new improcess -> imcontrol import
outside the one documented allowlist, would fail here.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]

# The single intentional improcess -> imcontrol edge: improcess reads the
# ``processing`` block from the shared ImSwitch setup file, whose format and
# selection are owned by imcontrol's config system. Kept lazy + guarded; see the
# module docstring of imswitch.improcess.model.processing_config.
_IMPROCESS_IMCONTROL_ALLOWLIST = {"imswitch/improcess/model/processing_config.py"}


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


def _package_files(dotted_package: str):
    """Yield (repo-relative posix path, source) for each .py file in a package,
    excluding test files."""
    root = _REPO_ROOT.joinpath(*dotted_package.split("."))
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if "/_test/" in rel or "/test_" in rel:
            continue
        yield rel, path.read_text(encoding="utf-8")


def _imports_of(source: str, *, prefix: str, top_level_only: bool):
    """Return module names imported from ``prefix`` in ``source``."""
    tree = ast.parse(source)
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(prefix))
    return found


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


# --- improcess -> imcontrol direction (edge inversion) ---


def test_improcess_has_no_top_level_imcontrol_imports():
    """improcess must be import-time independent of imcontrol: no top-level
    imcontrol import anywhere in the package."""
    offenders = {
        rel: sorted(_imports_of(src, prefix="imswitch.imcontrol", top_level_only=True))
        for rel, src in _package_files("imswitch.improcess")
    }
    offenders = {rel: mods for rel, mods in offenders.items() if mods}
    assert offenders == {}, f"top-level improcess->imcontrol imports: {offenders}"


def test_improcess_imcontrol_imports_are_allowlisted():
    """The only improcess -> imcontrol imports (even lazy) are the documented
    setup-config read. Any new edge must be inverted or explicitly allowlisted."""
    offenders = {
        rel: sorted(_imports_of(src, prefix="imswitch.imcontrol", top_level_only=False))
        for rel, src in _package_files("imswitch.improcess")
    }
    offenders = {
        rel: mods
        for rel, mods in offenders.items()
        if mods and rel not in _IMPROCESS_IMCONTROL_ALLOWLIST
    }
    assert offenders == {}, f"unexpected improcess->imcontrol imports: {offenders}"


def test_widget_state_persistence_lives_in_imcommon_without_imcontrol_dep():
    """The persistence service moved to imcommon and must not depend on
    imcontrol (which would re-introduce a core -> app edge)."""
    src = _module_path("imswitch.imcommon.model.WidgetStatePersistence").read_text(
        encoding="utf-8"
    )
    assert _imports_of(src, prefix="imswitch.imcontrol", top_level_only=False) == set()


def test_imcontrol_persistence_shim_preserves_identity():
    """The imcontrol re-export shim returns the same objects as imcommon, so the
    ~25 existing controller call sites are unaffected by the move."""
    from imswitch.imcommon.model import WidgetStatePersistence as CommonWSP
    from imswitch.imcommon.model import getWidgetStatePersistence as common_get
    from imswitch.imcontrol.model import WidgetStatePersistence as ShimWSP
    from imswitch.imcontrol.model import getWidgetStatePersistence as shim_get

    assert CommonWSP is ShimWSP
    assert common_get() is shim_get()
