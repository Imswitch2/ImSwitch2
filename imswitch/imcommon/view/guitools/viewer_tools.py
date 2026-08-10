"""Viewer-scoped broker for the shared interactive-tool layer.

Several panels (ROI manager, ROI statistics, Profile) let the user draw into a
napari viewer.  Each of them used to construct its own ``ViewerToolManager``,
and each manager lazily created its own ``"Viewer Tools"`` Shapes layer — so a
viewer ended up holding ``Viewer Tools``, ``Viewer Tools [1]`` and so on, and a
shape drawn for one panel was invisible to the others.

Collapsing that onto one layer is necessary but *not sufficient*: the panels
call ``clear_shapes()`` unconditionally and read "the first rectangle", so a
naively shared layer means Profile switching modes wipes the rectangle ROI
statistics is measuring.  Ownership is what makes sharing safe, so this service
owns the layer and hands out tokens:

* one service per viewer, keyed weakly so a closed viewer is not pinned;
* scratch shapes are tagged with the owner that drew them, and ``clear``/
  ``shapes`` only ever see that owner's shapes;
* every mutating call takes the ``ToolToken`` returned by :meth:`acquire`, and a
  token from a superseded acquisition raises :class:`StaleToolToken` rather than
  acting on somebody else's shapes;
* ``release`` disconnects every callback that owner registered, which is the
  only teardown hook available — panels have no close event to hang it on.

``ViewerToolManager`` is kept as the implementation underneath, unchanged, so
imcontrol's ``ImageWidget`` keeps working exactly as before.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass

from qtpy import QtCore

from .naparitools import ViewerToolManager


class StaleToolToken(RuntimeError):
    """Raised when a token from a superseded acquisition is used."""


@dataclass(frozen=True)
class ToolToken:
    """Proof that an owner currently holds the shared drawing tool.

    ``owner_key`` is the panel's stable identity — deliberately not
    ``id(widget)``, so a panel that is closed and reopened reclaims its own
    shapes instead of orphaning them.  ``generation`` increments on every
    acquisition, which is what makes a superseded token detectable.
    """

    owner_key: str
    generation: int


class ViewerToolService(QtCore.QObject):
    """Per-viewer owner of the scratch layer, the active tool and the target."""

    #: Emitted with the owner key that just lost the tool to someone else.
    sigToolPreempted = QtCore.Signal(str)
    #: Emitted when the image layer that measurements apply to changes.
    sigTargetLayerChanged = QtCore.Signal(object)

    _instances: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def __init__(self, viewer):
        super().__init__()
        self._viewer = viewer
        # Per-owner limits are applied here (see _enforce_single_per_owner), so
        # the manager's global "last rectangle wins" rule must be off: on a
        # shared layer it would let one panel's drawing delete another's.
        self._manager = ViewerToolManager(viewer, enforce_single=False)
        self._generations: dict[str, int] = {}
        self._owner_shapes: dict[str, list[int]] = {}
        self._callbacks: dict[str, list[tuple[object, object]]] = {}
        self._active_owner: str | None = None
        self._target_layer = None
        self._enforcing = False
        self._manager.sigShapesChanged.connect(self._on_shapes_changed)

    # -- construction -------------------------------------------------------

    @classmethod
    def for_viewer(cls, viewer) -> "ViewerToolService":
        """The service for ``viewer``, creating it on first use.

        Keyed weakly on the viewer so the service dies with it.  Viewers that
        cannot be weak-referenced (test doubles, mostly) get a fresh service,
        which is harmless because nothing else shares them.
        """
        try:
            existing = cls._instances.get(viewer)
        except TypeError:
            return cls(viewer)
        if existing is None:
            existing = cls(viewer)
            try:
                cls._instances[viewer] = existing
            except TypeError:
                pass
        return existing

    # -- token lifecycle ----------------------------------------------------

    def acquire(self, owner_key: str, mode: str | None = None) -> ToolToken:
        """Take the drawing tool for ``owner_key`` and return a fresh token."""
        previous = self._active_owner
        generation = self._generations.get(owner_key, 0) + 1
        self._generations[owner_key] = generation
        self._active_owner = owner_key
        token = ToolToken(owner_key=owner_key, generation=generation)
        if previous is not None and previous != owner_key:
            # Preemption never deletes the previous owner's shapes; it only
            # tells them to stop expecting further drawing events.
            self.sigToolPreempted.emit(previous)
        if mode is not None:
            self.set_mode(token, mode)
        return token

    def is_current(self, token: ToolToken) -> bool:
        return self._generations.get(token.owner_key) == token.generation

    def _check(self, token: ToolToken) -> None:
        if not self.is_current(token):
            raise StaleToolToken(
                f"tool token for {token.owner_key!r} (generation "
                f"{token.generation}) has been superseded"
            )

    def release(self, token: ToolToken) -> None:
        """Give up the tool, drop this owner's shapes and its callbacks.

        Idempotent, and releasing a superseded token is a no-op rather than an
        error: a panel closing after being preempted would otherwise raise on
        the way out.
        """
        if not self.is_current(token):
            return
        self._remove_owner_shapes(token.owner_key)
        self._disconnect_callbacks(token.owner_key)
        self._generations[token.owner_key] = token.generation + 1
        if self._active_owner == token.owner_key:
            self._active_owner = None

    # -- drawing ------------------------------------------------------------

    def set_mode(self, token: ToolToken, mode: str) -> None:
        self._check(token)
        self._active_owner = token.owner_key
        self._manager.set_mode(mode)

    def get_mode(self) -> str:
        return self._manager.get_mode()

    def shapes(self, token: ToolToken) -> list:
        """``(index, shape_type, vertices)`` for the shapes this owner drew."""
        self._check(token)
        data = self._manager.get_shapes_data()
        types = self._manager.get_shape_types()
        owned = self._owned_indices(token.owner_key, len(data))
        return [(i, types[i], data[i]) for i in owned if i < len(types)]

    def clear(self, token: ToolToken) -> None:
        """Remove only this owner's shapes."""
        self._check(token)
        self._remove_owner_shapes(token.owner_key)

    def share(self, token: ToolToken, dst_owner_key: str) -> None:
        """Hand this owner's shapes to another owner, explicitly."""
        self._check(token)
        self._owner_shapes[dst_owner_key] = list(
            self._owner_shapes.get(token.owner_key, [])
        )
        self._owner_shapes[token.owner_key] = []

    def claim_new_shapes(self, token: ToolToken) -> None:
        """Attribute any shapes not yet owned by anyone to this owner.

        The napari Shapes layer has no notion of who drew what, so ownership is
        recorded here whenever the panel next looks at the layer.
        """
        self._check(token)
        self._claim(token.owner_key)

    def _claim(self, owner_key: str) -> None:
        total = len(self._manager.get_shapes_data())
        claimed = {i for indices in self._owner_shapes.values() for i in indices}
        mine = self._owner_shapes.setdefault(owner_key, [])
        for index in range(total):
            if index not in claimed:
                mine.append(index)

    def _on_shapes_changed(self) -> None:
        """Attribute newly drawn shapes and apply the per-owner shape limit."""
        if self._enforcing or self._active_owner is None:
            return
        self._claim(self._active_owner)
        self._enforce_single_per_owner(self._active_owner)

    def _enforce_single_per_owner(self, owner_key: str) -> None:
        """Keep only the newest rectangle and line *this owner* drew.

        The same limit the manager used to apply globally, scoped so drawing in
        one panel cannot discard another panel's shape.
        """
        types = self._manager.get_shape_types()
        owned = [i for i in self._owner_shapes.get(owner_key, []) if i < len(types)]
        doomed: list[int] = []
        for shape_type in ("rectangle", "line"):
            of_type = [i for i in owned if types[i] == shape_type]
            doomed.extend(of_type[:-1])
        if not doomed:
            return
        self._enforcing = True
        try:
            for index in sorted(doomed, reverse=True):
                self._manager.remove_shape(index)
            self._reindex_after_removal(doomed, keep_owner=None)
        finally:
            self._enforcing = False

    # -- callbacks ----------------------------------------------------------

    def add_callback(self, token: ToolToken, signal, handler) -> None:
        """Connect ``handler`` to ``signal`` on this owner's behalf.

        Registered so :meth:`release` can disconnect it — panels have no
        teardown hook of their own, which is why handlers used to outlive them.
        """
        self._check(token)
        signal.connect(handler)
        self._callbacks.setdefault(token.owner_key, []).append((signal, handler))

    def _disconnect_callbacks(self, owner_key: str) -> None:
        for signal, handler in self._callbacks.pop(owner_key, []):
            try:
                signal.disconnect(handler)
            except (TypeError, RuntimeError):
                # Already gone (signal owner destroyed, or never connected).
                pass

    # -- target image layer -------------------------------------------------

    @property
    def target_image_layer(self):
        """The image layer measurements apply to.

        Panels used to each resolve this independently through
        ``active_image_layer()``, which falls back to the first image layer
        whenever the active layer is not an image — so clicking an annotation
        layer could silently change what a panel measured.
        """
        return self._target_layer

    @target_image_layer.setter
    def target_image_layer(self, layer) -> None:
        if layer is self._target_layer:
            return
        self._target_layer = layer
        self.sigTargetLayerChanged.emit(layer)

    # -- passthrough --------------------------------------------------------

    @property
    def manager(self) -> ViewerToolManager:
        """The underlying manager, for callers that need the raw layer."""
        return self._manager

    @property
    def sigShapesChanged(self):
        return self._manager.sigShapesChanged

    def get_rectangle_bounds(self, index: int):
        return self._manager.get_rectangle_bounds(index)

    def get_line_endpoints(self, index: int):
        return self._manager.get_line_endpoints(index)

    # -- internals ----------------------------------------------------------

    def _owned_indices(self, owner_key: str, total: int) -> list[int]:
        """This owner's shape indices, defaulting to all unattributed ones.

        Shapes drawn before anyone claimed them belong to the active owner;
        without that, the first draw after opening a panel would be invisible
        to it.
        """
        recorded = [i for i in self._owner_shapes.get(owner_key, []) if i < total]
        if recorded:
            return recorded
        if self._active_owner == owner_key:
            claimed = {i for indices in self._owner_shapes.values() for i in indices}
            return [i for i in range(total) if i not in claimed]
        return []

    def _remove_owner_shapes(self, owner_key: str) -> None:
        total = len(self._manager.get_shapes_data())
        doomed = sorted(self._owned_indices(owner_key, total), reverse=True)
        if not doomed:
            self._owner_shapes[owner_key] = []
            return
        self._enforcing = True
        try:
            for index in doomed:
                self._manager.remove_shape(index)
        finally:
            self._enforcing = False
        self._owner_shapes[owner_key] = []
        self._reindex_after_removal(doomed, keep_owner=owner_key)

    def _reindex_after_removal(self, doomed, keep_owner: str | None) -> None:
        """Shift recorded indices down past the shapes that were removed.

        Removing a shape renumbers everything above it, so ownership has to be
        rebased or it would start pointing at the wrong shapes.
        """
        doomed = set(doomed)
        for key, indices in list(self._owner_shapes.items()):
            if key == keep_owner:
                continue
            self._owner_shapes[key] = [
                index - sum(1 for d in doomed if d < index)
                for index in indices
                if index not in doomed
            ]


__all__ = ["StaleToolToken", "ToolToken", "ViewerToolService"]
