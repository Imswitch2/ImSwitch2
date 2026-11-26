"""etSTED on the whole application: the Advanced and Base panels, the mock
camera and APD, the simulated NI-DAQ.

These run the real main controller (``_etsted_rig.EtSTEDRig``), which stubs
the GUI dependencies process-wide and cannot be booted twice in a process, so
they live in modules pytest does not collect (``etsted_e2e_*.py``) and each
runs in a process of its own, like the GL-viewer tests. A failure shows the
child's report.

* ``etsted_e2e_advanced.py``: the triggered scan has the signals the Advanced
  panel would play; event -> scan one-shot / Endless; out-of-range and failed
  arm recover; cooldown and maximum; RecordingWidget initiation; the mock
  rehearsal of docs/gui.rst.
* ``etsted_e2e_base.py``: the Base panel's triggered scan makes the hardware
  calls it always did.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def _run(module):
    command = [
        sys.executable, '-m', 'pytest', '-p', 'pytestqt.plugin', '-p', 'pytest_timeout',
        '-p', 'no:cacheprovider', '--timeout=300', '-q', '-x', '-rf', str(HERE / module),
    ]
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen', MPLBACKEND='Agg',
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    done = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                          text=True, timeout=900)
    tail = '\n'.join((done.stdout + done.stderr).splitlines()[-60:])
    assert done.returncode == 0, f'{module} failed:\n{tail}'


# Each child takes ~2 minutes alone and more beside other subprocess tests: the
# lane's 180 s default would kill the parent while the child is still healthy.
@pytest.mark.timeout(900)
@pytest.mark.ui
def test_etsted_advanced_panel_whole_application():
    _run('etsted_e2e_advanced.py')


@pytest.mark.timeout(900)
@pytest.mark.ui
@pytest.mark.redzone
def test_etsted_base_panel_whole_application():
    _run('etsted_e2e_base.py')
