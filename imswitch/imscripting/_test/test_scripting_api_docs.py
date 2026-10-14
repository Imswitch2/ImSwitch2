"""docs/scripting-api.rst is generated from the controllers' ``@APIExport``
methods by tools/update_scripting_api_docs.py and committed, because Read
the Docs cannot import the controllers. This fails when the page is older
than the code, and when an exported method has no docstring."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.nohardware

REPO = Path(__file__).resolve().parents[3]


def test_the_reference_page_is_current():
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=str(REPO))
    completed = subprocess.run(
        [sys.executable, str(REPO / 'tools' / 'update_scripting_api_docs.py'), '--check'],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
