"""Tutorial timetagger 08 -- The frame clock and the pixel markers.

You will learn
  * the frame clock: one edge per frame, on which the card re-syncs its
    pixel index, so a lost line marker costs one frame and not the rest of
    the scan; its trigger level, swept between runs like the line clock's
  * ``skew()``: the *signed* time from the frame edge to the first line
    edge, both orders visible through a known added delay; it must stay
    below ``pattern_offset_ps()`` (the block's ``pixelPatternOffsetPs``),
    so the frame edge leads pixel 0 whatever the cable lengths
  * ``scope()``: one line's worth of frame, line and pixel-begin markers,
    as the card sees them
  * the last-frame check: the card closes the final frame by itself
    (``frame_closed_by_card`` in the products' metadata), whether the last
    pixel ends before or after the scan reports done

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: the frame clock on input 4, firing with the first line
                edge (skew 0); the pixel markers the FLIM detector
                generates 10 ns after each line edge.
  Your own microscope: the NI-DAQ's scan.frameStartClockLine cabled to the
                frameClockChannel input (optional: without it the card
                never re-syncs and this tutorial stops after the sweep).

Next: 09_line_delay_alignment.py
"""

import os

from imswitch.imcontrol.model.timeresolved import TimeResolvedScanConfig

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
APPLY = False
LEVELS_V = [0.2, 0.5, 1.0]
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')

if 'frame_clock' not in tt.roles():
    raise RuntimeError('This setup has no frame_clock role: cable scan.frameStartClockLine '
                       'to a card input and set frameClockChannel in the timeTagger block.')

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, False)
level0 = tt.channels()['frame_clock'].trigger_v
scan_s = design['Nx'] * design['Ny'] * design['dwell_s']
line_ps = design['Nx'] * design['dwell_s'] * 1e12
try:
    # The frame trigger level, swept between APD-only runs (as in 07). One
    # edge per scan is the whole signal: a level counts it or it does not.
    rows = []
    with tt.calibration('frame_sweep'):
        try:
            for level in LEVELS_V:
                tt.set_trigger_level('frame_clock', level, owner='frame_sweep')
                run = helpers.ScanRun()
                edges = tt.count_edges('frame_clock', duration_s=scan_s + 2.0, start=run.start)
                run.wait()
                rows.append((f'{level:+.2f} V', str(edges)))
        finally:
            tt.set_trigger_level('frame_clock', level0, owner='frame_sweep')
    helpers.printTable(rows, header=('frame trigger', 'frame edges in one scan'))
    seen = [lvl for lvl, row in zip(LEVELS_V, rows) if row[1] == '1']
    print(f"levels that see the one frame edge: {seen or 'none'}; the block has {level0:+.2f} V")
    print()

    # The signed frame -> line skew. A histogram only sees a line edge AFTER
    # the frame edge, so skew() delays the line input by 50 ns for the
    # measurement and subtracts that again: a line edge up to 50 ns before
    # the frame edge then reads negative instead of vanishing. It is a
    # conditioning write, so the card must be free: FLIM is disabled.
    run = helpers.ScanRun()
    skew = tt.skew('frame_clock', 'line_clock', duration_s=scan_s + 2.0,
                   added_delay_ps=50_000, start=run.start)
    run.wait()
    print(skew.summary())
    offset = tt.pattern_offset_ps('line_clock')
    print(f'pixel markers start {offset} ps after a line edge (pixelPatternOffsetPs')
    print('plus a positive lineClockDelayPs), so the frame edge must come less than')
    print(f'that after the line edge. Frame leads pixel 0: {skew.frame_leads_pixel_0}')
    if not skew.frame_leads_pixel_0:
        print('  -> raise pixelPatternOffsetPs above the skew (it only delays the markers')
        print('     by nanoseconds) or shorten the frame clock cable.')
    print()

    # Now with the FLIM detector in the scan: the pixel markers exist, the
    # scope shows one line of them, and the final frame must close.
    helpers.setFlimEnabled(DETECTOR, True)
    facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
    tr = facade.time_resolved
    token = tr.configure(TimeResolvedScanConfig(), owner='tutorial08') if tr else None
    try:
        run = helpers.ScanRun()
        trace = tt.scope(['frame_clock', 'line_clock'], trigger_role='frame_clock',
                         window_ps=int(line_ps * 1.2), duration_s=scan_s + 2.0,
                         detector_name=DETECTOR, start=run.start)
        run.wait()
        print('scope, from the frame edge, one line (times in us; rising edges only):')
        for name, events in trace.items():
            rising = [t for t, state in events if state == 'rising']
            shown = ', '.join(f'{t / 1e6:.3f}' for t in rising[:6])
            more = f' ... ({len(rising)} edges)' if len(rising) > 6 else ''
            print(f'  {name:12s} {shown}{more}')
        print()
        if tr is not None:
            products = tr.wait_for_final(timeout_s=30, owner=token)
            closed = products.metadata.get('frame_closed_by_card')
            print(f'final frame closed by the card: {closed}')
            if closed:
                print('  -> the card counted the last pixel end before the detector gave up:')
                print('     the markers and the frame clock are complete.')
            else:
                print('  -> the detector read the frame after a grace period: the card never')
                print('     counted the last pixel end. A marker is missing (tutorial 07) or')
                print('     the frame edge arrived after pixel 0 (the skew above).')
    finally:
        if tr is not None:
            tr.clear(token)
    print()
    print('APPLY writes nothing here: the frame trigger level follows tutorial 02\'s')
    print('rule, and pixelPatternOffsetPs is a block field (default 10000 ps).')
finally:
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
