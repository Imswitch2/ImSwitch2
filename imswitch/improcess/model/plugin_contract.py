"""The headless parameter contract a plugin declares, and whether it holds.

A plugin's ``default_params()`` is the root of everything that runs without
its widget: the keys a workflow may set, the values a headless run starts
from, and what the provenance records as the effective parameters. The
framework base classes return an empty dict so that old plugins keep
working in the GUI, but an inherited empty dict is *not* a declaration --
it says nothing about what the widget hands ``apply``. So:

* a plugin that does not override ``default_params()`` has **no contract**:
  it is GUI-only. Workflow validation refuses it, ``workflows list`` says
  so, and the provenance of its GUI results marks the step non-replayable
  with that reason;
* a plugin whose widget disagrees with its declaration has a **mismatch**.
  The GUI checks this once, on the widget it builds anyway, and remembers
  the outcome on the class so later results carry the reason too.

Nothing here builds a widget or imports Qt: the registry stays headless,
and a standalone process trusts the explicit declaration. The widget check
runs where the widget is already being constructed on the GUI thread, in
CI over the built-ins and examples, and through :func:`check_plugin_contract`
for third-party plugin tests.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Per-class memo of the last widget check: ``""`` when it passed, the
#: joined problems when it did not. Absent until a widget was checked.
_CHECK_ATTR = "_param_contract_problem"


def framework_base(cls):
    """The framework class whose ``default_params`` counts as "undeclared"."""
    from imswitch.improcess.processors.base import Processor
    from imswitch.improcess.reconstructors.base import Reconstructor

    for base in (Processor, Reconstructor):
        if isinstance(cls, type) and issubclass(cls, base):
            return base
    return None


def has_param_contract(plugin_or_cls) -> bool:
    """Whether the plugin's ``default_params`` is an override, resolved through MRO.

    Any override above the framework base counts, so a plugin family may
    declare its contract on its own intermediate base class.
    """
    cls = plugin_or_cls if isinstance(plugin_or_cls, type) else type(plugin_or_cls)
    base = framework_base(cls)
    resolved = getattr(cls, "default_params", None)
    if base is None:
        return callable(resolved)
    inherited = getattr(base, "default_params", None)
    return getattr(resolved, "__func__", resolved) is not getattr(inherited, "__func__", inherited)


def contract_problem(plugin_or_cls) -> str | None:
    """Why the plugin cannot be trusted headlessly, or ``None`` when it can.

    Either the contract is undeclared (see :func:`has_param_contract`) or a
    widget check recorded a mismatch (see :func:`record_widget_check`).
    """
    cls = plugin_or_cls if isinstance(plugin_or_cls, type) else type(plugin_or_cls)
    pid = str(getattr(cls, "id", "") or cls.__name__)
    if not has_param_contract(cls):
        return (
            f"plugin {pid!r} declares no parameter contract (default_params is not "
            "overridden); it is GUI-only"
        )
    recorded = cls.__dict__.get(_CHECK_ATTR, "")
    if recorded:
        return f"plugin {pid!r}: its parameter widget disagrees with default_params(): {recorded}"
    return None


def widget_problems(plugin, widget) -> list[str]:
    """Every way an already-built widget disagrees with the declaration.

    Checks the key sets, every non-volatile value, and that the declared
    defaults survive the plugin's own codec. Never builds anything.
    """
    cls = type(plugin)
    problems: list[str] = []
    if not has_param_contract(cls):
        problems.append("default_params is not overridden")
    getter = getattr(widget, "get_values", None)
    if not callable(getter):
        return [*problems, "the parameter widget has no get_values()"]
    try:
        values = dict(getter())
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return [*problems, f"get_values() failed: {exc}"]
    try:
        defaults = dict(cls.default_params())
    except Exception as exc:  # noqa: BLE001
        return [*problems, f"default_params() failed: {exc}"]
    only_widget = sorted(set(values) - set(defaults))
    only_declared = sorted(set(defaults) - set(values))
    if only_widget:
        problems.append(f"widget keys missing from default_params(): {only_widget}")
    if only_declared:
        problems.append(f"declared keys the widget does not provide: {only_declared}")
    volatile = set(getattr(cls, "default_params_volatile", ()) or ())
    for key in sorted((set(values) & set(defaults)) - volatile):
        if not _same(values[key], defaults[key]):
            problems.append(f"{key!r}: widget default {values[key]!r} != declared {defaults[key]!r}")
    try:
        encoded, reasons = plugin.encode_params(defaults)
        if reasons:
            problems.append(f"defaults do not encode losslessly: {reasons}")
        elif not _same(plugin.decode_params(encoded), defaults):
            problems.append("defaults do not survive the codec round trip")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"codec failed on the defaults: {exc}")
    return problems


def record_widget_check(plugin, widget) -> list[str]:
    """Run :func:`widget_problems` and remember the outcome on the class.

    Called where the GUI builds the widget anyway. A recorded mismatch is
    what :func:`contract_problem` reports afterwards, so the provenance of
    results made with that plugin says why they are not replayable.
    """
    problems = widget_problems(plugin, widget)
    try:
        setattr(type(plugin), _CHECK_ATTR, "; ".join(problems))
    except Exception:  # pragma: no cover - a frozen class
        pass
    return problems


def warn_contract_problems(logger, plugin, widget) -> list[str]:
    """GUI entry point: check the widget just built and log every problem loudly.

    A drop-in's mismatch is a warning (the GUI keeps working, the results
    say why they are not replayable); the built-ins are pinned by a test.
    """
    problems = record_widget_check(plugin, widget)
    if problems:
        pid = str(getattr(plugin, "id", "") or type(plugin).__name__)
        logger.warning(
            "Plugin %r: parameter widget and default_params() disagree; results made with it "
            "are recorded as NOT replayable and it cannot run in a workflow until fixed: %s",
            pid, "; ".join(problems),
        )
    return problems


def check_plugin_contract(plugin_cls, parent=None) -> list[str]:
    """Author-facing check: build the widget and compare it with the declaration.

    For a plugin's own test suite::

        from imswitch.improcess.model.plugin_contract import check_plugin_contract

        def test_contract(qapp):
            assert check_plugin_contract(MyProcessor) == []

    Needs a Qt application, like any widget. Returns the problems; an empty
    list means the contract holds. Also records the outcome on the class.
    """
    plugin = plugin_cls()
    if parent is None:
        from qtpy import QtWidgets

        parent = QtWidgets.QWidget()
    widget = plugin.make_param_widget(parent)
    if widget is None:
        # A plugin driven from a widget it does not own (legacy MoNaLISA):
        # only the declaration itself can be checked.
        return [] if has_param_contract(plugin_cls) else ["default_params is not overridden"]
    return record_widget_check(plugin, widget)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        try:
            return bool(np.array_equal(np.asarray(a), np.asarray(b)))
        except Exception:
            return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    try:
        return bool(a == b)
    except Exception:
        return False


__all__ = [
    "check_plugin_contract",
    "contract_problem",
    "has_param_contract",
    "record_widget_check",
    "warn_contract_problems",
    "widget_problems",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
