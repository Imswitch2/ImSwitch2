"""Fixtures: a real MMCore (pymmcore-plus) with the pip ``mm-test-adapters`` DemoCamera devices."""
import os
import sys

import pytest

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
