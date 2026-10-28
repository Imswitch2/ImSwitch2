"""Fixtures: a real MMCore (pymmcore-plus) with the pip ``mm-test-adapters`` DemoCamera devices."""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

# The app tests (test_e2e_app.py) boot ImControl, and importing imswitch syncs
# ~/ImSwitchConfig. Point HOME at a scratch folder before anything imports
# imswitch, so the whole plugin test session leaves the real user files alone.
# (If imswitch is already imported this is too late; e2e_helpers.boot then moves
# the user-files folder itself. Windows ignores HOME for the Documents folder.)
if "imswitch" not in sys.modules and os.environ.get("IMSWITCH_MM_TESTS_KEEP_HOME") != "1":
    _scratch_home = tempfile.mkdtemp(prefix="imswitch-mm-tests-home-")
    os.environ["HOME"] = _scratch_home
    atexit.register(shutil.rmtree, _scratch_home, ignore_errors=True)

# The repo uses importlib import mode, which does not put this folder on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("pymmcore_plus")
mm_test_adapters = pytest.importorskip("mm_test_adapters")

from imswitch_device_micromanager._core import MMCoreHost  # noqa: E402
from mm_helpers import DEMO_CONFIG, make_demo_cameras  # noqa: E402


@pytest.fixture
def adapter_dir():
    return mm_test_adapters.device_adapter_path()


@pytest.fixture
def host(adapter_dir):
    h = MMCoreHost("test", config_file=DEMO_CONFIG, adapter_paths=(adapter_dir,))
    h.connect()
    try:
        make_demo_cameras(h)
        yield h
    finally:
        h.shutdown()
