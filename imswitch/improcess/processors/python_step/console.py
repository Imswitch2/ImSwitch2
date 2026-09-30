"""What the ImProcess console sees: the Python step's namespace over the live list.

Qt-free, like :mod:`.context`, so the rules can be tested without a window.
The console's widget owns one dict, the namespace its commands run in;
:class:`ConsoleSession` fills it.

It is the step's namespace (``np``, ``data``, ``inputs``, ``axes``, ``scales``,
``unit``, ``axis``, ``results``, ``make_result``, ``make_labels``) bound to
what is selected in the results list, less ``outputs`` / ``out``, which
nothing in a console reads, plus three functions for the list itself:

* ``current()`` -- the result shown now, or ``None``;
* ``selected()`` -- the results selected in the list, in list order;
* ``publish(array_or_result, *, name=None, axes=None, scales=None, like=None)``
  -- add a result to the list.

A result made this way records an ``opaque`` provenance node ("made in the
console"): the console cannot know which lines made it, so the node claims no
inputs and is not replayable. The Python step is the recorded form.
"""

from __future__ import annotations

import numpy as np

from imswitch.improcess.model.provenance import graph_of, record_console_result
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.python_step.context import (
    build_namespace,
    make_labels,
    make_result,
    result_from_value,
)

#: The names :meth:`ConsoleSession.refresh` rebinds when the selection changes;
#: any other name in the namespace is the user's and is never touched.
REBOUND_NAMES = ("data", "inputs", "axes", "scales", "unit", "axis", "results")
#: The step's namespace has these for the code to set; a console has no use for them.
_STEP_ONLY_NAMES = ("outputs", "out")


class ConsoleSession:
    """Fills and refreshes a console namespace from the live results list.

    ``selected()`` and ``current()`` answer with the results selected and
    shown now; ``publish(result, name)`` adds one to the list. All three are
    supplied by the controller, so this object knows nothing of Qt or of the
    communication channel.
    """

    def __init__(self, *, selected, current, publish, namespace: dict | None = None):
        self._selected = selected
        self._current = current
        self._publish = publish
        self.namespace: dict = namespace if namespace is not None else {}
        self._bound: list[ProcessingResult] = []
        self._signature: tuple | None = None
        self._frozen = 0
        self.namespace.update(
            {
                "np": np,
                "make_result": make_result,
                "make_labels": make_labels,
                "current": self.current,
                "selected": self.selected,
                "publish": self.publish,
            }
        )
        self.refresh()

    # -- the live list ------------------------------------------------------------

    def current(self) -> ProcessingResult | None:
        """The result shown now, or ``None``."""
        return self._current()

    def selected(self) -> list[ProcessingResult]:
        """The results selected in the list, in list order."""
        return list(self._selected())

    def bound(self) -> list[ProcessingResult]:
        """The results ``data``, ``inputs`` and ``results`` are bound to now."""
        return list(self._bound)

    def _chosen(self) -> list[ProcessingResult]:
        chosen = [result for result in self.selected() if result is not None]
        if not chosen:
            shown = self.current()
            chosen = [shown] if shown is not None else []
        return chosen

    def refresh(self) -> list[ProcessingResult]:
        """Rebind ``data`` and the other reserved names to the selection.

        The selection, or the current result when nothing is selected. A result
        that is not in memory stays the lazy array it is (``np.asarray(data)``
        reads it), so following the selection never reads a whole recording.
        Returns the results now bound.
        """
        chosen = self._chosen()
        self._bound = chosen
        self._signature = tuple(id(result) for result in chosen)
        if chosen:
            fresh = build_namespace(chosen, materialise=False)
            for name in REBOUND_NAMES:
                self.namespace[name] = fresh[name]
        else:
            self.namespace.update(
                {"data": None, "inputs": [], "axes": [], "scales": [], "unit": "px", "results": []}
            )
            self.namespace["axis"] = _no_axes
        return list(chosen)

    def refresh_if_changed(self) -> bool:
        """:meth:`refresh`, but only when the selection is not what ``data`` is bound to.

        What the controller calls on every selection change and before every
        command: rebinding on each command would undo a user's own
        ``data = data[0]``, so nothing is touched while the selection is the
        same, nor while a ``publish`` is in progress. Returns whether it rebound.
        """
        if self._frozen:
            return False
        if tuple(id(result) for result in self._chosen()) == self._signature:
            return False
        self.refresh()
        return True

    # -- publishing ---------------------------------------------------------------

    def publish(self, value, *, name=None, axes=None, scales=None, like=None) -> ProcessingResult:
        """Add ``value`` to the results list and return the result made.

        ``value`` is an array, the output of ``make_result`` / ``make_labels``, or
        a result. An array of the dimensionality of ``like`` (by default the
        first result ``data`` is bound to) inherits its axes, scales and unit;
        any other says its ``axes`` (and optionally ``scales``) itself.
        """
        reference = like if like is not None else (self._bound[0] if self._bound else None)
        if isinstance(value, ProcessingResult):
            result = value
            if name is not None:
                result.name = str(name)
        else:
            default = f"{reference.name} (console)" if reference is not None else "console result"
            result = result_from_value(
                value, reference,
                what="the published array",
                default_name=default, name=name, axes=axes, scales=scales,
                metadata={"operation": "console"},
            )
        if graph_of(result) is None:
            # A result that already has a history keeps it; only a new one is "made here".
            record_console_result(result, selection=self._bound)
        # Adding to the list moves the selection; ``data`` must not move under a
        # script that publishes twice, so it is rebound at the next command instead.
        self._frozen += 1
        try:
            self._publish(result, getattr(result, "name", "") or "console result")
        finally:
            self._frozen -= 1
        return result


def _no_axes(label_or_index):
    raise ValueError(
        "no result is selected, so there are no axes: select one in the results list "
        "(then data, axes and axis() follow it)"
    )


__all__ = ["ConsoleSession", "REBOUND_NAMES"]
