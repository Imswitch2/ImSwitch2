"""Tutorial timetagger 07 -- The line clock during a scan.

You will learn
  * that the scan clocks fire only while a scan runs, so their checks run
    one -- with the FLIM detector disabled, so the APD images and the card
    stays free for a calibration meanwhile
  * ``count_edges()``: the line edges the card saw over a whole scan,
    against the Ny lines the scan designed (a line the card misses is a
    line missing from every FLIM image)
  * ``period()``: the line period (dwell x Nx plus the flyback) and its
    jitter, measured while the scan runs
  * a trigger-level sweep of the line clock between runs: a DAQ line is
    about 1 V into 50 ohm, so the plateau is narrow and 0.5 V sits in it
  * more edges than lines: the clock pulse's falling edge bouncing back
    through the threshold, seen with ``scope()``, and the dead time that
    swallows the bounces (``lineClockDeadtimePs``)

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a 64 x 64 pixel galvo scan at 400 us per pixel (about 2 s)
                whose line and frame clocks the simulated card derives from
                the designed TTL waveforms, so the edge count and the period
                match the design and the jitter is the card's own floor.
  Your own microscope: scan_params/flim_scan_64px.json is loaded for the
                runs and the previous Scan widget settings put back. The
                NI-DAQ's scan.lineClockLine must be cabled to the
                lineClockChannel input, into 50 ohm.

Next: 08_frame_clock_and_pixel_markers.py
"""

import os

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
APPLY = False
LEVELS_V = [0.2, 0.5, 1.0]          # the sweep: tutorial 02's rule is 0.5 V
# Tried when the card counts more edges than lines: longer than the clock
# pulse (10 DAQ samples: 100 us at 100 kS/s) and far below any line period.
DEADTIME_US = 200
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, False)   # the APD images; the card is free
level0 = tt.channels()['line_clock'].trigger_v
deadtime0 = tt.channels()['line_clock'].deadtime_ps
deadtime_suggested = None
try:
    scan_s = design['Nx'] * design['Ny'] * design['dwell_s']
    line_design_ps = design['Nx'] * design['dwell_s'] * 1e12
    print(f"design: {design['Ny']} lines of {design['Nx']} pixels at "
          f"{design['dwell_s'] * 1e6:.0f} us -> a line is at least "
          f"{line_design_ps / 1e6:.3f} ms, the scan at least {scan_s:.2f} s")
    print()

    # Run 1: every line edge of one scan. The counter is started first and
    # the scan from inside it (start=run.start), so line 0 is counted too;
    # it runs 2 s longer than the scan to be sure to see its end.
    run = helpers.ScanRun()
    edges = tt.count_edges('line_clock', duration_s=scan_s + 2.0, start=run.start)
    run.wait()
    expected = design['Ny'] * design['n_linesteps']
    print(f'line edges in one scan: {edges} (designed: {expected})')
    if edges == expected:
        print('  -> every line reaches the card.')
    elif edges == 0:
        print('  -> no edges at all: the line clock is not cabled to this input, or')
        print('     the threshold is above what the DAQ line drives into 50 ohm.')
    elif edges < expected:
        print('  -> lines are missing: a marginal threshold (the sweep below), or')
        print('     a cable picking up noise.')
    else:
        # More edges than lines. A DAQ line into the card's 50 ohm input has
        # a slow falling edge that bounces back through the threshold, and
        # every bounce is another "line start": the FLIM pixel pattern is
        # re-triggered mid-line and the frame never closes cleanly. The
        # scope shows it: edges clustered just after each pulse's end.
        print(f'  -> {edges - expected} more edges than lines. A scope of one line:')
        run = helpers.ScanRun()
        trace = tt.scope(['line_clock'], 'line_clock', window_ps=int(line_design_ps * 1.5),
                         duration_s=scan_s + 2.0, start=run.start)
        run.wait()
        rows, last = [], None
        for t, state in trace['line_clock']:
            gap = '' if last is None else f'+{(t - last) / 1e6:.3f} us'
            rows.append((f'{t / 1e6:.3f} us', state, gap))
            last = t
        helpers.printTable(rows, header=('after line edge', 'edge', 'gap'))
        print('     (a clean pulse is one rising edge and one falling edge per line;')
        print('      a cluster of edges just after the falling edge is its bounce)')
        print()
        # The remedy is a dead time on the input: longer than the pulse and
        # its bounce, far below the line period. Tried inside a calibration
        # transaction, like the sweep; the card keeps it only with APPLY.
        with tt.calibration('line_deadtime'):
            try:
                tt.set_deadtime('line_clock', DEADTIME_US * 1000, owner='line_deadtime')
                run = helpers.ScanRun()
                again = tt.count_edges('line_clock', duration_s=scan_s + 2.0, start=run.start)
                run.wait()
            finally:
                tt.set_deadtime('line_clock', deadtime0, owner='line_deadtime')
        print(f'  with a {DEADTIME_US} us dead time on the line clock: {again} edges '
              f'(designed: {expected})')
        if again == expected:
            deadtime_suggested = DEADTIME_US * 1000
            print('  -> the dead time swallows the bounces. Suggested: '
                  f'lineClockDeadtimePs = {deadtime_suggested} (the frame clock takes it too).')
        else:
            print('  -> still off: raise DEADTIME_US (stay below the line period), or look at')
            print('     the cable and its termination; the sweep below shows the plateau.')
    print()

    # Run 2: the period while the scan runs. The design gives the histogram
    # its size; the measurement adds the flyback and the card's jitter.
    period = helpers.runScanMeasuring(
        lambda: tt.period('line_clock', duration_s=0.5,
                          expected_period_ps=line_design_ps * 1.5))
    print(period.summary())
    if period.n_periods:
        flyback = period.period_ps - line_design_ps
        print(f'  -> {flyback / 1e6:.3f} ms per line beyond Nx x dwell: the flyback and')
        print(f'     settling the scan designer adds. Jitter {period.jitter_ps:.0f} ps RMS:')
        print('     a DAQ clock at its sample rate is exact; what you see is the card.')
    print()

    # Runs 3 to 5: the trigger level, swept between runs. The sweep owns the
    # card's conditioning meanwhile, so no FLIM scan can start on a
    # temporary level -- the APD-only scan does not touch the card.
    rows = []
    with tt.calibration('line_sweep'):
        try:
            for level in LEVELS_V:
                tt.set_trigger_level('line_clock', level, owner='line_sweep')
                rate = helpers.runScanMeasuring(
                    lambda: tt.count_rates(['line_clock'], duration_s=0.5).rates_hz['line_clock'])
                rows.append((f'{level:+.2f} V', f'{rate:,.1f} Hz'))
        finally:
            tt.set_trigger_level('line_clock', level0, owner='line_sweep')
    helpers.printTable(rows, header=('line trigger', 'line rate during the scan'))
    rates = [float(r[1].split()[0].replace(',', '')) for r in rows]
    top = max(rates) if rates else 0.0
    plateau = [lvl for lvl, r in zip(LEVELS_V, rates) if top > 0 and r >= 0.9 * top]
    print()
    if not plateau:
        print('No level counts: wrong input, wrong polarity (a negative channel number')
        print('selects the falling edge) or the DAQ line into a high-impedance input.')
        chosen = level0
    else:
        chosen = 0.5 if 0.5 in plateau else plateau[len(plateau) // 2]
        print(f'plateau at {", ".join(f"{v:+.2f}" for v in plateau)} V: the DAQ line drives')
        print(f'about 1 V into 50 ohm, so {chosen:+.2f} V sits safely inside it.')
    print()
    block = {'lineClockTriggerV': chosen}
    if deadtime_suggested is not None:
        block['lineClockDeadtimePs'] = deadtime_suggested
    if APPLY and (chosen != level0 or deadtime_suggested is not None):
        if chosen != level0:
            tt.set_trigger_level('line_clock', chosen)
        if deadtime_suggested is not None:
            tt.set_deadtime('line_clock', deadtime_suggested)
        print('Applied until restart; keep it in the timeTagger block:')
    else:
        print(f'Not applied (APPLY is False or unchanged): lineClockTriggerV stays {level0}, '
              f'lineClockDeadtimePs {deadtime0}. For the timeTagger block:')
    print('  ' + ', '.join(f'"{k}": {v}' for k, v in block.items()))
finally:
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
