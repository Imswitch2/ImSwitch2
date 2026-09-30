"""Generate the scripting API reference pages in ``docs/api``.

Reads the ``@APIExport`` markers straight from the classes, so no GUI, setup
file or hardware is needed:

* ``api.imcontrol.rst`` — every method any ImControl controller exports, with
  the widget that provides it.  A running ``api.imcontrol`` holds the methods
  of the widgets its setup loads (plus the ones that are always there), so the
  page lists the union and says which widget each one needs.
* ``_actions.rst`` — the global helper functions of the scripting scope.
* ``mainWindow.rst`` — the ``mainWindow`` object.

Run from the repository root::

    HOME=$(mktemp -d) python tools/generateapidocs.py

(Importing ImSwitch2 syncs the user folder in ``HOME``; a throwaway one keeps
the run from touching your own ``ImSwitchConfig``.)
"""

import importlib
import inspect
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from imswitch.imcommon.controller import MultiModuleWindowController  # noqa: E402
from imswitch.imscripting.model.actions import _Actions  # noqa: E402

docsDir = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../docs')
apiDocsDir = os.path.join(docsDir, 'api')

#: Objects ImConMainController always adds to the API, whatever the setup loads.
_ALWAYS_LOADED = {
    'CommunicationChannel': 'imswitch.imcontrol.controller.CommunicationChannel',
    'SetupModeController': 'imswitch.imcontrol.controller.SetupModeController',
    'WorkflowFacadeController': 'imswitch.imcontrol.controller.controllers.WorkflowFacadeController',
}


def _indent(text, spaces):
    return '\n'.join(f'{" " * spaces}{line}' if line.strip() else ''
                     for line in text.splitlines())


def _rst(docstring):
    """A docstring as reStructuredText.

    Many controller docstrings are Google style (``Args:``, ``Returns:``),
    which only reads as RST after napoleon's conversion, the same one
    ``sphinx.ext.napoleon`` applies to autodoc pages.
    """
    try:
        from sphinx.ext.napoleon import Config
        from sphinx.ext.napoleon.docstring import GoogleDocstring
    except ImportError:
        return docstring
    return str(GoogleDocstring(docstring, Config(napoleon_use_param=True, napoleon_use_rtype=True)))


def _signature(func):
    """The signature a script calls, without ``self``."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return '(...)'
    parameters = [p for name, p in signature.parameters.items() if name != 'self']
    return str(signature.replace(parameters=parameters))


def _exported(cls):
    """(name, function) of every API-exported method on ``cls``, inherited too."""
    for name in dir(cls):
        if name.startswith('_'):
            continue
        member = getattr(cls, name, None)
        if callable(member) and getattr(member, '_APIExport', False):
            yield name, member


def _providerName(cls):
    name = cls.__name__
    return name[:-len('Controller')] if name.endswith('Controller') else name


def _imcontrolExports():
    """name -> (function, providers).

    A provider is the widget key a controller is loaded for (``Laser``,
    ``ScanAdvanced`` for the Advanced scan widget), or ``None`` for the
    objects that are always loaded. The controllers are the ones ImControl can
    load: the ``controllers`` package lists them, and ImConMainController
    loads ``<key>Controller`` (``ScanController<type>`` for the scan widget).
    """
    from imswitch.imcontrol.controller import controllers

    classes = {}
    for className in controllers._CONTROLLER_MODULES:
        if className in _ALWAYS_LOADED:
            continue
        if className.startswith('ScanController'):
            provider = 'Scan' + className[len('ScanController'):]
        else:
            provider = className[:-len('Controller')]
        classes[getattr(controllers, className)] = provider
    for className, moduleName in _ALWAYS_LOADED.items():
        classes[getattr(importlib.import_module(moduleName), className)] = None

    exports = {}
    for cls, provider in classes.items():
        for name, func in _exported(cls):
            entry = exports.setdefault(name, [func, set()])
            entry[1].add(provider)
    return exports


def _scanWidgets(providers):
    return {p for p in providers if p and p.startswith('Scan')}


def _availability(providers):
    if None in providers:
        return 'Always available.'
    widgets = sorted(providers - _scanWidgets(providers))
    scans = sorted(_scanWidgets(providers))
    parts = []
    if widgets:
        parts.append(' or '.join(f'*{w}*' for w in widgets))
    if scans:
        kinds = ', '.join(s[len('Scan'):] or 'Base' for s in scans)
        parts.append(f'the *Scan* widget ({kinds})')
    return 'Needs ' + ' or '.join(parts) + ' in ``availableWidgets``.'


def writeImcontrol():
    exports = _imcontrolExports()
    title = 'api.imcontrol'
    lines = ['*' * len(title), title, '*' * len(title), '',
             '.. This page is generated by tools/generateapidocs.py; edit the',
             '   docstrings in the controllers, not this file.', '',
             f'.. class:: {title}', '',
             '   These functions are available in the ``api.imcontrol`` object of a',
             '   script.  Which ones a session has depends on the widgets its setup',
             '   file loads; each entry says what it needs.  Calling one the setup',
             '   does not provide raises ``AttributeError`` with that explanation.', '']
    for name in sorted(exports, key=str.lower):
        func, providers = exports[name]
        lines.append(f'   .. method:: {name}{_signature(func)}')
        lines.append('')
        doc = inspect.getdoc(func)
        if doc:
            lines.append(_indent(_rst(doc).rstrip(), 6))
            lines.append('')
        lines.append(_indent(_availability(providers), 6))
        lines.append('')
    with open(os.path.join(apiDocsDir, f'{title}.rst'), 'w') as file:
        file.write('\n'.join(lines).rstrip() + '\n')
    return len(exports)


def writeDocs(cls, isClass=True, displayName=None):
    def fixIndent(docstring, indent):
        lines = docstring.splitlines()
        for i in range(len(lines)):
            lines[i] = f'{" " * indent}{lines[i].lstrip()}'
        return '\n'.join(lines)

    rst = ''
    indent = 0

    if not displayName:
        displayName = cls.__name__

    # Title
    title = displayName
    rst += f'{"*" * len(title)}\n'
    rst += f'{title}\n'
    rst += f'{"*" * len(title)}\n'
    rst += '\n'

    # Class
    if isClass:
        rst += f'.. class:: {displayName}\n'
        indent += 3
        if cls.__doc__:
            rst += '\n'
            rst += f'{fixIndent(cls.__doc__, indent)}\n'
        rst += '\n'

    # Attributes
    for attrName in dir(cls):
        if attrName.startswith('_'):
            continue  # Skip private members

        attr = getattr(cls, attrName)
        if callable(attr):
            # Method
            rst += f'{" " * indent}.. method:: {attr.__name__}{_signature(attr)}\n'
            indent += 3
            if attr.__doc__:
                rst += '\n'
                rst += f'{fixIndent(attr.__doc__, indent)}\n'
            rst += '\n'
            indent -= 3

    # End class
    if isClass:
        indent -= 3

    # Write rst file
    with open(os.path.join(apiDocsDir, f'{cls.__name__}.rst'), 'w') as file:
        file.write(rst)


def main():
    os.makedirs(apiDocsDir, exist_ok=True)
    count = writeImcontrol()
    print(f'api.imcontrol.rst: {count} methods')

    # Global-level functions
    class _actions:
        """ These functions are available at the global level. """
        pass

    for subObjName in dir(_Actions):
        subObj = getattr(_Actions, subObjName)
        if hasattr(subObj, '_APIExport') and subObj._APIExport:
            setattr(_actions, subObjName, subObj)

    writeDocs(_actions, isClass=False, displayName='Global-level functions')

    # mainWindow
    class mainWindow:
        """ These functions are available in the mainWindow object. """
        pass

    for subObjName in dir(MultiModuleWindowController):
        subObj = getattr(MultiModuleWindowController, subObjName)
        if hasattr(subObj, '_APIExport') and subObj._APIExport:
            setattr(mainWindow, subObjName, subObj)

    writeDocs(mainWindow)


if __name__ == '__main__':
    main()


# Copyright (C) 2020-2021 ImSwitch developers
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
