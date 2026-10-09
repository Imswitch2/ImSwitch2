"""Helpers for the Time Tagger tutorials -- not a script to run on its own.

Imported by: 02_trigger_levels_and_dead_time.py

(01 and 03 to 10 use it too; 07 to 09 use the scan helpers at the end.)

The tutorials reach the card through the workflow facade:
``api.imcontrol.buildWorkflowFacade().time_tagger``. It is ``None`` on a
setup without a ``timeTagger`` block, so every tutorial starts with
``findTimeTagger()``, which says what is missing instead of failing on
``None`` three lines later.
"""


def findTimeTagger():
    """The Time Tagger facade of the loaded setup, or a clear error."""
    facade = api.imcontrol.buildWorkflowFacade()
    tt = facade.time_tagger
    if tt is None:
        raise RuntimeError(
            'This setup has no top-level "timeTagger" block, so there is no '
            'Time Tagger to talk to. Load galvo_flim_mock_scan_setup.json for '
            'the simulated card, or add the block to your own setup file (see '
            'the Time Tagger chapter of the documentation).'
        )
    if not tt.connected:
        raise RuntimeError(
            'The Time Tagger is configured but not connected: the vendor '
            'library is missing or the card is not on the USB. On a setup '
            'without a card, set "simulation": true in the timeTagger block.'
        )
    return tt


def findFlimDetector():
    """The name of the detector that reads the card (``click_role`` is its
    tell), or None."""
    for name in api.imcontrol.getDetectorNames():
        if 'click_role' in api.imcontrol.getDetectorParameters(name):
            return name
    return None


def printTable(rows, header=None):
    """Print aligned columns. ``rows`` is a list of tuples of strings."""
    rows = [tuple(str(cell) for cell in row) for row in rows]
    if header:
        rows = [tuple(header)] + rows
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for k, row in enumerate(rows):
        print('  '.join(cell.ljust(width) for cell, width in zip(row, widths)))
        if header and k == 0:
            print('  '.join('-' * width for width in widths))


def describeCard(tt):
    """One line about the card and one per role."""
    kind = 'simulated card' if tt.is_mock else 'card'
    print(f'Time Tagger: {tt.model} (serial {tt.serial}), {kind}, '
          f'TCSPC direction: {tt.tcspc_direction}')
    rows = [(role, f'{c.channel:+d}', c.edge, f'{c.trigger_v:+.2f} V',
             f'{c.deadtime_ps / 1000:g} ns', f'{c.delay_ps} ps')
            for role, c in tt.channels().items()]
    printTable(rows, header=('role', 'channel', 'edge', 'trigger', 'dead time', 'delay'))


# --------------------------------------------------------------------------- #
# Scans for the scan-aware tutorials (07 to 09)                                #
# --------------------------------------------------------------------------- #

import json as _json
import os as _os
import tempfile as _tempfile


def loadScanParams(path):
    """Back up the Scan widget's settings, load ``path`` into it, and return
    ``(backupPath, design)``: ``design`` has ``Nx``, ``Ny``, ``dwell_s`` and
    ``n_linesteps`` from the file. Put the backup back in a ``finally``
    with ``api.imcontrol.loadScanParamsFromFile(backupPath)``."""
    backup = _os.path.join(_tempfile.gettempdir(), 'imswitch_timetagger_scan_backup.json')
    api.imcontrol.saveScanParamsToFile(backup)
    api.imcontrol.loadScanParamsFromFile(path)
    with open(path, encoding='utf-8') as file:
        settings = _json.load(file)
    digital = settings['digitalParameterDict']
    design = {
        'Nx': int(digital['Nx']), 'Ny': int(digital['Ny']),
        'dwell_s': float(digital['sequence_time']),
        'n_linesteps': int(digital.get('n_linesteps', 1)),
    }
    return backup, design


def setFlimEnabled(detector, enabled):
    """Switch the FLIM detector's participation in scans; returns the previous
    setting (a string, 'True' or 'False'). With it off, the APD images and
    the card is not held, so a calibration can run during the scan."""
    if detector is None:
        return None
    previous = api.imcontrol.getDetectorParameter(detector, 'enabled')
    api.imcontrol.setDetectorParameter(detector, 'enabled', 'True' if enabled else 'False')
    return previous


def restoreFlimEnabled(detector, previous):
    if detector is not None and previous is not None:
        api.imcontrol.setDetectorParameter(detector, 'enabled', previous)


class ScanRun:
    """A scan started without waiting, to measure while it runs::

        run = helpers.ScanRun()
        run.start()                 # returns once the scan's clocks run
        ... measure ...
        run.wait()                  # raises if the scan failed

    ``start`` can also be handed to a measurement that must span the scan
    from its first edge: ``tt.count_edges('line_clock', 3.0, start=run.start)``.
    ``prepare`` requests the scan and returns once it is *built* (the FLIM
    detector's pixel markers exist, the clocks do not run yet); ``start``
    after it waits for the clocks: ``tt.scope(..., prepare=run.prepare,
    start=run.start)``.
    """

    def __init__(self, timeout=120):
        self.timeout = timeout
        self.handle = None
        self._started = None
        self._built = None

    def prepare(self):
        signals = api.imcontrol.signals()
        self._built = getWaitForSignal(signals.scanBuilt, timeout=60)
        self._started = getWaitForSignal(signals.scanStarted, timeout=60)
        self.handle = api.imcontrol.runScan()
        self._built()

    def start(self):
        if self.handle is None:
            signals = api.imcontrol.signals()
            self._started = getWaitForSignal(signals.scanStarted, timeout=60)
            self.handle = api.imcontrol.runScan()
        self._started()

    def wait(self):
        if self.handle is None:
            raise RuntimeError('ScanRun.start() was not called')
        if not self.handle.wait(self.timeout):
            raise TimeoutError(f'the scan did not end within {self.timeout} s')
        if not self.handle.successful:
            raise RuntimeError(f'the scan failed: {self.handle.message}')


def runScanMeasuring(measure, timeout=120):
    """Start a scan, call ``measure()`` while it runs, wait for the scan to
    end and return what ``measure`` returned."""
    run = ScanRun(timeout)
    run.start()
    try:
        result = measure()
    finally:
        run.wait()
    return result


def imageShift(measured, truth):
    """By how many pixels along x the measured image is the truth shifted,
    and how well they then match: ``measured[:, k] ~ truth[:, k + shift]``.
    Every shift within a quarter of the width is tried on the whole image
    (the two must have the same shape)."""
    import numpy as np
    a = np.asarray(measured, dtype=float)
    b = np.asarray(truth, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f'shapes differ: {a.shape} vs {b.shape}')
    a = (a - a.mean()).ravel()
    if not a.any() or not (b - b.mean()).any():
        return 0, 0.0
    best, best_r = 0, -2.0
    for shift in range(-(b.shape[1] // 4), b.shape[1] // 4 + 1):
        rolled = np.roll(b, -shift, axis=1)
        r = float(np.corrcoef(a, (rolled - rolled.mean()).ravel())[0, 1])
        if r > best_r:
            best, best_r = shift, r
    return best, best_r
