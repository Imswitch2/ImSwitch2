"""The categories ImProcess sorts its analysis tools into, in the order they are shown.

A processor names its category in its ``category`` class attribute; the
analysis-tool menus (*Tools ▾* in the toolbar, *Tools › Analysis tools* in the
menu bar) show one submenu per category in :data:`TOOL_CATEGORIES` order, and
the workflow editor's palette follows the same order. The order runs roughly
the way an analysis does: get the data into shape, clean it up, find things
in it, measure them, then show and script.

Built-in tools must use one of these names (a test checks it, so a typo does
not quietly open a new submenu). A drop-in plugin may name any category: one
that is not listed here is shown after the listed ones, alphabetically, and
:data:`OTHER_CATEGORY` always comes last.
"""

from __future__ import annotations

TOOL_CATEGORIES: tuple[str, ...] = (
    "Dimensions and channels",  # crop, split, combine, project
    "Transform",                # rotate/flip, resize, convert type
    "Filters",                  # spatial filters, background subtraction
    "Math",                     # arithmetic on one or two images
    "Restoration",              # deconvolution, denoising
    "Registration",             # drift correction, multicolor alignment
    "Segmentation",             # thresholds, labels
    "Measurement",              # colocalization, region properties
    "Resolution and PSF",       # FRC, bead PSF measurements
    "Spots and points",         # spot finding and counting, picking points
    "SMLM",                     # localization tables
    "Visualization",            # composites, RGB
    "ROI",                      # region-of-interest panels
    "Exploration",              # graph, profile, metadata panels
    "Scripting",                # Python step, console
)

#: Where a tool without a category goes; always shown last.
OTHER_CATEGORY = "Other"


def category_sort_key(category: str | None) -> tuple[int, int, str]:
    """Sort key putting the listed categories first, in order, then the rest
    alphabetically, then :data:`OTHER_CATEGORY`."""
    name = str(category or OTHER_CATEGORY).strip() or OTHER_CATEGORY
    if name in TOOL_CATEGORIES:
        return (0, TOOL_CATEGORIES.index(name), "")
    if name == OTHER_CATEGORY:
        return (2, 0, "")
    return (1, 0, name.casefold())


__all__ = ["OTHER_CATEGORY", "TOOL_CATEGORIES", "category_sort_key"]
