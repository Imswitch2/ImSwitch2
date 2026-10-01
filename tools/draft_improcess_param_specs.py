"""Draft ``param_spec()`` declarations for ImProcess plugins from their widgets.

Builds each plugin's parameter widget offscreen, perturbs every control and
watches ``get_values()`` to learn which key each control feeds, then prints
a ``param_spec()`` classmethod to paste into the plugin: the choices behind
a combo box, the bounds, step and unit of a spin box, the label and the
tooltip. A key the probe cannot tie to exactly one control (a check box
that blanks a group of spin boxes, a preset list that moves several keys)
is emitted as the plain field derived from its default, marked ``TODO``,
for the author to describe by hand.

Usage
-----

Draft every installed plugin, built-in and drop-in::

    python tools/draft_improcess_param_specs.py

One or more plugins by id::

    python tools/draft_improcess_param_specs.py filter smlm-localizer example.gaussian-blur

The report of what the probe could and could not read, without the code::

    python tools/draft_improcess_param_specs.py --report
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plugin", nargs="*", help="plugin ids (default: every installed plugin)")
    parser.add_argument("--report", action="store_true", help="what the probe read, without the draft")
    parser.add_argument("--no-user-plugins", action="store_true", help="leave out the drop-in folder")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from qtpy import QtWidgets

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from imswitch.improcess.model.param_probe import draft_spec_source, probe_plugin
    from imswitch.improcess.workflows.runtime import bootstrap_registry

    registry = bootstrap_registry(user_plugins=not args.no_user_plugins)
    plugins = list(registry.reconstructors()) + list(registry.processors())
    wanted = set(args.plugin)
    unknown = wanted - {plugin.id for plugin in plugins}
    if unknown:
        print(f"unknown plugin id(s): {sorted(unknown)}", file=sys.stderr)
        return 2
    for plugin in plugins:
        if wanted and plugin.id not in wanted:
            continue
        probed = probe_plugin(plugin)
        print(f"# {plugin.id}  ({type(plugin).__module__}.{type(plugin).__name__})")
        for note in probed.notes:
            print(f"#   note: {note}")
        for key, controls in probed.composite.items():
            print(f"#   {key}: fed by several controls ({', '.join(controls)})")
        if args.report:
            for key, seen in probed.fields.items():
                extra = f" options={list(seen.options)}" if seen.options else ""
                bounds = f" [{seen.min}, {seen.max}]" if seen.min is not None or seen.max is not None else ""
                print(f"#   {key}: {seen.type}{extra}{bounds}{' ' + seen.suffix if seen.suffix else ''}")
        else:
            print(draft_spec_source(type(plugin), probed))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())


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
