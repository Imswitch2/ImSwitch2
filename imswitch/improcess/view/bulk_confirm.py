"""Confirmation guard before an operation floods the viewer.

Operations like Split stack (one result per plane) or Make composite (one
napari layer per channel) can silently explode into hundreds of layers on an
accidental click — napari takes ages or locks up. Both publish paths
(ImageToolbarController and ResultProcessorController) call
``confirm_bulk_publish`` before emitting, so anything above the threshold asks
first. Publishing also auto-selects the newest result, which immediately
renders its display layers — which is why the per-result layer count is
checked here too, not at render time.
"""

from __future__ import annotations

from qtpy import QtWidgets

#: Above this many new results (or viewer layers for one result) we ask first.
BULK_PUBLISH_THRESHOLD = 10


def bulk_publish_description(results, threshold: int = BULK_PUBLISH_THRESHOLD) -> str | None:
    """Return a warning line when publishing would flood napari, else ``None``.

    Checks the number of results being published at once (Split stack on a
    long axis) and each result's display-layer count (Make composite on a
    many-channel axis). Metadata-only: display-layer specs wrap views, no
    pixel data is copied.
    """
    results = list(results)
    if len(results) > threshold:
        return (
            f"This operation will add {len(results)} new results "
            f"to the reconstruction list."
        )
    for result in results:
        layers_fn = getattr(result, "display_layers", None)
        if not callable(layers_fn):
            continue
        try:
            layer_count = len(layers_fn())
        except Exception:
            continue
        if layer_count > threshold:
            name = getattr(result, "name", "This result")
            return f"'{name}' will render {layer_count} viewer layers at once."
    return None


def confirm_bulk_publish(
    parent,
    results,
    threshold: int = BULK_PUBLISH_THRESHOLD,
) -> bool:
    """Ask before publishing a flood of results/layers; True means proceed.

    Under-threshold publishes return True without any dialog.
    """
    message = bulk_publish_description(results, threshold)
    if message is None:
        return True
    reply = QtWidgets.QMessageBox.question(
        parent if isinstance(parent, QtWidgets.QWidget) else None,
        "Create many layers?",
        message
        + "\n\nCreating many napari layers can take a long time or freeze "
        "the viewer. Continue?",
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return reply == QtWidgets.QMessageBox.Yes


__all__ = [
    "BULK_PUBLISH_THRESHOLD",
    "bulk_publish_description",
    "confirm_bulk_publish",
]
