"""Helpers for the Time Tagger tutorials -- not a script to run on its own.

Imported by: 02_trigger_levels_and_dead_time.py

(03 to 10 use it too.)

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
