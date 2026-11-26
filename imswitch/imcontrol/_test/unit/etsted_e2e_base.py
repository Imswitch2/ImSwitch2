"""etSTED's triggered scan on the BASE scan panel is what it always was.

The triggered scan is now built by the active scan source. For the Base
panel that must be exactly the scan manager's ``makeFullScan(analog, digital,
False)``, the call etSTED made before. Both ways are run through the whole
application (simulated NI-DAQ) and what the hardware layer was asked is
compared: the same calls in the same order with the same signals.
"""
import numpy as np
import pytest

from ._etsted_rig import EtSTEDRig, Session, same


def _base_setup(setup):
    setup['scan'].update({
        'scanWidgetType': 'Base',
        'scanDesigner': 'BetaScanDesigner',
        'scanDesignerParams': {'return_time': 0.001},
        'TTLCycleDesigner': 'BetaTTLCycleDesigner',
    })
    setup['positioners']['Z']['managerProperties']['minVolt'] = -10   # a Z scan about 0


@pytest.fixture(scope='module')
def rig(tmp_path_factory):
    rig = EtSTEDRig(tmp_path_factory.mktemp('etsted_base'), setup_edit=_base_setup)
    yield rig
    rig.close()


def _snapshot(value, depth=0):
    """Arrays by value, containers recursively, anything else by its repr."""
    if depth > 6:
        return repr(type(value))
    if isinstance(value, np.ndarray):
        return value.copy()
    if isinstance(value, dict):
        return {key: _snapshot(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot(item, depth + 1) for item in value]
    if isinstance(value, (bool, int, float, str, type(None), np.generic)):
        return value
    return repr(type(value))


def _small_scan(rig):
    """The Base panel opens with a 50 x 50 x 50 scan of 10 ms pixels: minutes."""
    fields = rig.scan._widget.scanPar
    for name, value in (('sizeX', '1'), ('sizeY', '1'), ('sizeZ', '0.1'), ('seqTime', '5')):
        fields[name].setText(value)
    rig.scan.getParameters()


def _run_once(rig, use_source, monkeypatch):
    """One event -> scan; returns the hardware-facing call log."""
    log = []
    nidaq = rig.master.nidaqManager
    manager = rig.master.scanManager

    def spy(name, function):
        def call(*args, **kwargs):
            log.append((name, _snapshot(args), _snapshot(kwargs)))
            return function(*args, **kwargs)
        return call

    monkeypatch.setattr(manager, 'makeFullScan', spy('makeFullScan', manager.makeFullScan))
    monkeypatch.setattr(nidaq, 'runScan', spy('runScan', nidaq.runScan))
    monkeypatch.setattr(nidaq, 'resolveScanTTLDevices',
                        spy('resolveScanTTLDevices', nidaq.resolveScanTTLDevices))
    if not use_source:
        monkeypatch.setattr(rig.etsted, '_resolveScanSource', lambda: None)
    session = Session(rig, prepare_scan=_small_scan)
    try:
        session.start_detecting()
        session.trigger()
        assert rig.wait_for(lambda: session.apd_frames, 40), session.statuses
        assert rig.wait_for(lambda: not rig.etsted._experimentActive, 20)
        log.extend(('setDigital', name, level) for name, level in session.digital.items())
    finally:
        session.close()
        monkeypatch.undo()
    return log


def test_base_triggered_scan_hardware_calls_equal_the_scan_manager_path(rig, monkeypatch):
    with_source = _run_once(rig, True, monkeypatch)
    without = _run_once(rig, False, monkeypatch)
    assert [entry[0] for entry in with_source] == [entry[0] for entry in without]
    assert [entry[0] for entry in with_source].count('makeFullScan') == 1
    same([entry[1:] for entry in with_source], [entry[1:] for entry in without])
    assert any(entry[0] == 'runScan' for entry in with_source)
