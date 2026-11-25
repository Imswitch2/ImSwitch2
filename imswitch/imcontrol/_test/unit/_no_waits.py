"""Workflow tests drive the mock facade, which needs none of the workflows'
exposure and settle waits (``time.sleep`` of up to the acquisition duration)."""

import importlib
import time
from types import SimpleNamespace

import pytest


def no_waits(*workflow_modules):
    """An autouse fixture that gives each named module of
    ``imswitch.imcontrol.model.workflows`` a ``time`` whose ``sleep`` returns at
    once. Only those modules see it; sleeps elsewhere (threads, Qt) are real."""

    @pytest.fixture(autouse=True)
    def _no_waits(monkeypatch):
        for name in workflow_modules:
            module = importlib.import_module(f"imswitch.imcontrol.model.workflows.{name}")
            fast = SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
            fast.sleep = lambda _seconds: None
            monkeypatch.setattr(module, "time", fast)

    return _no_waits
