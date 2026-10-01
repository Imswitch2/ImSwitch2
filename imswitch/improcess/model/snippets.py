"""Snippets: Python-step code kept in ``~/ImSwitchConfig/improcess_snippets``.

A one-off that turns out to be used twice is saved here and loaded back into
the Python step's panel. A snippet is a plain ``.py`` file whose first line
may name the step's output ports::

    # ports: a, b
    ax = 0
    ...

The header is the only thing that is not the code itself; it is stripped on
load, so what the panel shows is exactly what ``save_snippet`` was given.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from imswitch.imcommon.model import dirtools

_SNIPPETS_SUBDIR = "improcess_snippets"
_SUFFIX = ".py"
_HEADER = re.compile(r"^#\s*ports\s*:(.*)$", re.IGNORECASE)
#: What a snippet name may contain: it becomes a file name, so no separators,
#: no leading dot and nothing a common file system refuses.
_NAME = re.compile(r"^[\w][\w ()+.\-]*$")


def snippets_directory(*, create: bool = True) -> str:
    """The snippet folder, created on first use when ``create``."""
    directory = os.path.join(dirtools.UserFileDirs.Root, _SNIPPETS_SUBDIR)
    if create:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            # A read-only home must not break the panel; saving then says why.
            pass
    return directory


def _path_for(name: str) -> Path:
    """The file behind a snippet called ``name`` (``.py`` optional)."""
    stem = str(name or "").strip()
    if stem.lower().endswith(_SUFFIX):
        stem = stem[: -len(_SUFFIX)]
    if not stem or not _NAME.match(stem) or stem.endswith((" ", ".")):
        raise ValueError(
            f"{name!r} is not a usable snippet name: use letters, digits, spaces, "
            "'_', '-', '.', '(', ')' or '+', starting with a letter or digit"
        )
    return Path(snippets_directory(create=False)) / f"{stem}{_SUFFIX}"


def list_snippets() -> list[str]:
    """The names (file stems) of the saved snippets, sorted."""
    directory = Path(snippets_directory(create=False))
    try:
        return sorted(path.stem for path in directory.glob(f"*{_SUFFIX}") if path.is_file())
    except OSError:
        return []


def load_snippet(name: str) -> tuple[str, str]:
    """``(code, ports)`` of the snippet ``name``.

    A first line ``# ports: a, b`` gives the ports and is removed from the
    code; without one the ports are the step's default. Raises
    ``FileNotFoundError`` for a snippet that does not exist and ``ValueError``
    for a name that cannot be one.
    """
    path = _path_for(name)
    if not path.is_file():
        raise FileNotFoundError(f"no snippet called {name!r} in {path.parent}")
    with open(path, encoding="utf-8", newline="") as handle:
        return parse_snippet(handle.read())


def parse_snippet(text: str) -> tuple[str, str]:
    """``(code, ports)`` of a snippet's text: the ``# ports:`` first line, if any, is the ports.

    Pure, so a tool that reads the shipped snippets (the one that writes their
    workflow files) splits them exactly as the panel does.
    """
    from imswitch.improcess.processors.python_step.context import DEFAULT_PORTS

    lines = text.splitlines(keepends=True)
    if lines:
        match = _HEADER.match(lines[0].rstrip("\r\n"))
        if match:
            return "".join(lines[1:]), match.group(1).strip() or DEFAULT_PORTS
    return text, DEFAULT_PORTS


def save_snippet(name: str, code: str, ports: str, *, overwrite: bool = True) -> Path:
    """Write ``code`` and its ``ports`` as the snippet ``name``; returns the file.

    Replaces a snippet of the same name unless ``overwrite`` is false, in which
    case ``FileExistsError`` says so and nothing is written.
    """
    from imswitch.improcess.processors.python_step.context import DEFAULT_PORTS

    path = _path_for(name)
    ports = str(ports or "").strip() or DEFAULT_PORTS
    if "\n" in ports or "\r" in ports:
        raise ValueError("the output ports must be on one line")
    if not overwrite and path.exists():
        raise FileExistsError(f"a snippet called {path.stem!r} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(f"# ports: {ports}\n{code}")
    return path


__all__ = ["list_snippets", "load_snippet", "parse_snippet", "save_snippet", "snippets_directory"]
