"""Write a panel added at run time into a setup file -- and nothing else.

``configfiletools.saveSetupInfo`` writes the whole in-memory setup back: it
reorders keys, spells out defaults, drops keys a typed section does not
know, and takes along every other unsaved change of the session (laser
presets, ROIs, shortcuts). That is not what "save this panel to my setup"
means, so this module patches the file as it is on disk instead:

* the raw JSON is read, and only ``availableWidgets`` and the panel's own
  section are changed -- every other key, its order and its value stay as
  the file had them, unknown keys included;
* the result is written to a temporary file next to the target and moved
  into place with ``os.replace``, so the file is never seen half-written;
* :func:`saveAsNewSetup` writes a new file into the setup folder (into a
  subfolder when the name has one) and refuses to overwrite an existing one.

Neither function changes which setup ImSwitch starts with; that is
``configfiletools.saveSetupChoice``, and only when the operator asks for it.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from . import setupfolder


@dataclass
class PatchResult:
    path: str
    """ The file written. """

    changes: List[str] = field(default_factory=list)
    """ What changed, in words ("added Tiling to availableWidgets", ...). """


def _readJson(path):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f'{path} does not hold a setup (a JSON object)')
    return data, text


def _indentOf(text: str):
    """The indent the file uses, so that the rewrite looks like it."""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(' \t')
        if stripped and len(stripped) != len(line):
            prefix = line[:len(line) - len(stripped)]
            return '\t' if prefix.startswith('\t') else len(prefix)
    return 4


def applyPanelPatch(data: dict, *, addWidgets: Iterable[str] = (),
                    sections: Optional[Dict[str, dict]] = None) -> List[str]:
    """Change ``data`` (a parsed setup file) in place: list ``addWidgets`` in
    ``availableWidgets`` and set each of ``sections``. Returns what changed."""
    changes = []
    addWidgets = list(addWidgets)
    available = data.get('availableWidgets', None)
    if available is True:
        pass  # every panel already
    else:
        if not isinstance(available, list):
            available = []
        missing = [key for key in addWidgets if key not in available]
        if missing or 'availableWidgets' not in data:
            data['availableWidgets'] = list(available) + missing
            for key in missing:
                changes.append(f'added {key} to availableWidgets')
    for key, value in (sections or {}).items():
        if data.get(key) != value:
            changes.append(('replaced' if key in data else 'added')
                           + f' the {key} section')
            data[key] = value
    return changes


def _writeAtomically(path: str, data: dict, *, indent, trailingNewline: bool,
                     overwrite: bool) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if trailingNewline:
        text += '\n'
    handle, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                                         suffix='.tmp', dir=folder)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            if not overwrite:
                raise FileExistsError(f'{path} already exists')
            try:
                os.chmod(temporary, os.stat(path).st_mode & 0o7777)
            except OSError:
                pass
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def patchSetupFile(path: str, *, addWidgets: Iterable[str] = (),
                   sections: Optional[Dict[str, dict]] = None) -> PatchResult:
    """Add panels (and their sections) to the setup file at ``path``,
    changing nothing else in it. Nothing is written when nothing changes."""
    data, text = _readJson(path)
    changes = applyPanelPatch(data, addWidgets=addWidgets, sections=sections)
    if changes:
        _writeAtomically(path, data, indent=_indentOf(text),
                         trailingNewline=text.endswith('\n'), overwrite=True)
    return PatchResult(path=str(path), changes=changes)


def newSetupPath(folder: str, name: str) -> str:
    """Where ``name`` -- ``my_setup``, ``my_setup.json`` or
    ``lab/my_setup.json`` -- goes in the setup folder ``folder``. Raises
    ValueError for a name that is empty or leads out of the folder, and
    FileExistsError for one that is taken."""
    name = str(name or '').strip().replace('\\', '/')
    if not name or name.endswith('/'):
        raise ValueError('Give the new setup a name.')
    if os.path.isabs(name):
        raise ValueError('Name the new setup relative to the setup folder, '
                         'e.g. "lab/my_setup".')
    if not name.lower().endswith('.json'):
        name += '.json'
    path = setupfolder.setupFilePath(folder, name)
    if not setupfolder.isInside(path, folder):
        raise ValueError(f'"{name}" is not inside the setup folder {folder}.')
    if os.path.exists(path):
        raise FileExistsError(f'There already is a setup "{name}" in {folder}; '
                              f'choose another name.')
    return path


def saveAsNewSetup(sourcePath: str, folder: str, name: str, *,
                   addWidgets: Iterable[str] = (),
                   sections: Optional[Dict[str, dict]] = None) -> PatchResult:
    """Write a copy of the setup file at ``sourcePath`` -- as it is on disk,
    plus the panel -- as a new setup ``name`` in the setup folder ``folder``.
    Refuses to overwrite. The result's ``path`` is the new file."""
    path = newSetupPath(folder, name)
    data, text = _readJson(sourcePath)
    changes = applyPanelPatch(data, addWidgets=addWidgets, sections=sections)
    _writeAtomically(path, data, indent=_indentOf(text),
                     trailingNewline=text.endswith('\n'), overwrite=False)
    return PatchResult(path=path, changes=[f'copied {os.path.basename(sourcePath)}']
                       + changes)


def readSection(path: str, key: str) -> Optional[dict]:
    """The section ``key`` as the setup file at ``path`` writes it, or None.
    The configuration form starts from this, so that keys it does not show
    survive an edit."""
    try:
        data, _ = _readJson(path)
    except Exception:  # noqa: BLE001 -- no file, or not one we can read
        return None
    value = data.get(key)
    return value if isinstance(value, dict) else None
