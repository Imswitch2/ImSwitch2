#!/usr/bin/env python3
"""Standalone launcher for the ImSwitch Config Studio.

The editor itself now lives in :mod:`imswitch.imcontrol.view.configeditor` so
that it ships with every install and imcontrol can open it in-process from
``Tools > Edit hardware configuration...``. This file stays behind because the
documented way to edit a setup file without starting the microscope is still::

    python utility_scripts/imswitch_config_editor.py [/path/to/setup/dir]
"""

import sys
from pathlib import Path

# Running this file directly does not put the repo root on sys.path, and a
# source checkout need not be pip-installed. An installed ImSwitch already
# resolves the import and the insert is harmless.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from imswitch.imcontrol.view.configeditor import main  # noqa: E402

if __name__ == '__main__':
    main()
