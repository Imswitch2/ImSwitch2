"""docs/api, the scripting API reference, is generated from the
``@APIExport`` methods by tools/generateapidocs.py and committed (Read the
Docs does not run the generator). This fails when a page is older than the
code, and when a function a script can call has no docstring."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.nohardware

REPO = Path(__file__).resolve().parents[3]


def test_the_reference_is_current_and_every_export_is_documented():
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=str(REPO),
               HOME=tempfile.mkdtemp())
    completed = subprocess.run(
        [sys.executable, str(REPO / 'tools' / 'generateapidocs.py'), '--check'],
        capture_output=True, text=True, env=env, timeout=300, cwd=str(REPO),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
