"""Keeps CommunicationChannel's signal docstrings honest.

Each ``sigX = Signal(...)`` in ``CommunicationChannel`` carries an attribute
docstring (PEP 258) ending in ``Emitters:`` / ``Listeners:`` lines that name the
classes wiring themselves to it. Those lists are documentation, so nothing stops
them rotting the moment someone adds a ``connect`` -- which is what these tests
are for: they re-derive the real wiring from the AST of ``improcess`` and fail
when a docstring drifts, when a signal is undocumented, or when a signal loses
all emitters or all listeners (i.e. becomes dead).

Two details worth knowing if you touch this file:

* Attribute docstrings do **not** exist at runtime -- the string literal after
  an assignment is discarded by the interpreter, so ``sigX.__doc__`` returns
  ``Signal``'s docstring, not ours. Everything here therefore parses source with
  ``ast`` rather than introspecting the class.
* Widgets define their own signals that share names with channel signals (e.g.
  ``MulticolorWidget.sigResultProduced``, bridged onto the channel by
  ``ImProcessMainController._wire_producing_panel``). The scan only counts calls
  whose receiver looks like a comm-channel reference, so those widget-local
  signals are not misattributed to the channel.
"""

import ast
import collections
import inspect
import pathlib
import re

IMPROCESS_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHANNEL_PATH = IMPROCESS_ROOT / "controller" / "CommunicationChannel.py"

# Receivers that count as "the CommunicationChannel": self._commChannel,
# self.__commChannel, comm_channel, commChannel -- but not `widget` or `self`.
_CHANNEL_RECEIVER = re.compile(r"comm_?channel$", re.IGNORECASE)
_LIST_KEY = re.compile(r"^(Emitters|Listeners):\s*(.*)$")


# --------------------------------------------------------------- doc parsing

def _split_classes(text: str) -> set[str]:
    return {part.strip() for part in text.split(",") if part.strip()}


def _parse_doc_lists(doc: str) -> dict[str, set[str]]:
    """Pull ``Emitters:``/``Listeners:`` lists out of one attribute docstring.

    A key line starts a section; following indented non-blank lines continue it,
    so a long list may wrap across lines.
    """
    found: dict[str, set[str]] = {}
    key, buf = None, []

    def flush():
        if key is not None:
            found[key] = _split_classes(" ".join(buf))

    for line in inspect.cleandoc(doc).splitlines():
        match = _LIST_KEY.match(line)
        if match:
            flush()
            key, buf = match.group(1), [match.group(2)]
        elif key is not None and line.strip() and line[:1].isspace():
            buf.append(line.strip())
        elif key is not None:
            flush()
            key, buf = None, []
    flush()
    return found


def _documented_signals() -> dict[str, str | None]:
    """Map every ``sigX = Signal(...)`` in the channel to its attribute docstring."""
    tree = ast.parse(CHANNEL_PATH.read_text(encoding="utf-8"), str(CHANNEL_PATH))
    class_def = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CommunicationChannel"
    )

    signals: dict[str, str | None] = {}
    body = class_def.body
    for index, node in enumerate(body):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id.startswith("sig")):
            continue
        if not (isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Signal"):
            continue

        doc = None
        following = body[index + 1] if index + 1 < len(body) else None
        if (isinstance(following, ast.Expr)
                and isinstance(following.value, ast.Constant)
                and isinstance(following.value.value, str)):
            doc = following.value.value
        signals[target.id] = doc
    return signals


# ------------------------------------------------------------- source scanning

def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _scan_wiring(signal_names) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Find which classes emit/connect each channel signal in production code."""
    emitters: dict[str, set[str]] = collections.defaultdict(set)
    listeners: dict[str, set[str]] = collections.defaultdict(set)
    names = set(signal_names)

    for path in IMPROCESS_ROOT.rglob("*.py"):
        if path == CHANNEL_PATH or "_test" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), str(path))
        class_stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_ClassDef(self, node):
                class_stack.append(node.name)
                self.generic_visit(node)
                class_stack.pop()

            def visit_Call(self, node):
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and func.attr in ("emit", "connect")
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr in names
                        and _CHANNEL_RECEIVER.search(_receiver_name(func.value.value))):
                    owner = class_stack[-1] if class_stack else path.stem
                    bucket = emitters if func.attr == "emit" else listeners
                    bucket[func.value.attr].add(owner)
                self.generic_visit(node)

        Visitor().visit(tree)

    return emitters, listeners


# ------------------------------------------------------------------- the tests

def test_every_signal_has_a_docstring():
    """A new signal must document what it carries and who uses it."""
    undocumented = sorted(
        name for name, doc in _documented_signals().items() if not (doc or "").strip()
    )
    assert undocumented == [], (
        f"CommunicationChannel signals missing an attribute docstring: {undocumented}"
    )


def test_every_signal_docstring_declares_emitters_and_listeners():
    missing = []
    for name, doc in _documented_signals().items():
        lists = _parse_doc_lists(doc or "")
        for key in ("Emitters", "Listeners"):
            if not lists.get(key):
                missing.append(f"{name}: no '{key}:' line")
    assert missing == [], "\n".join(missing)


def test_documented_emitters_match_the_source():
    signals = _documented_signals()
    emitters, _ = _scan_wiring(signals)

    drift = []
    for name, doc in signals.items():
        documented = _parse_doc_lists(doc or "").get("Emitters", set())
        actual = emitters[name]
        if documented != actual:
            drift.append(
                f"{name}\n"
                f"    documented: {sorted(documented)}\n"
                f"    in source : {sorted(actual)}"
            )
    assert drift == [], "Emitters lists are out of date:\n" + "\n".join(drift)


def test_documented_listeners_match_the_source():
    signals = _documented_signals()
    _, listeners = _scan_wiring(signals)

    drift = []
    for name, doc in signals.items():
        documented = _parse_doc_lists(doc or "").get("Listeners", set())
        actual = listeners[name]
        if documented != actual:
            drift.append(
                f"{name}\n"
                f"    documented: {sorted(documented)}\n"
                f"    in source : {sorted(actual)}"
            )
    assert drift == [], "Listeners lists are out of date:\n" + "\n".join(drift)


def test_no_signal_is_dead():
    """A signal with no emitter (or no listener) is dead wiring -- remove it.

    This is the check that caught ``sigAddToMultiData``, which had a listener in
    MultiDataFrameController and no emitter anywhere in the repo.
    """
    signals = _documented_signals()
    emitters, listeners = _scan_wiring(signals)

    never_emitted = sorted(name for name in signals if not emitters[name])
    never_heard = sorted(name for name in signals if not listeners[name])

    assert never_emitted == [], f"signals nothing emits: {never_emitted}"
    assert never_heard == [], f"signals nothing listens to: {never_heard}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))


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
