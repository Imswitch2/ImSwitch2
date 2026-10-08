"""Tutorial timetagger 09 -- Line delay: aligning the FLIM image to the APD.

You will learn
  * why the FLIM image can sit a few pixels off along x against the APD
    image: the line clock marks the galvo's *command*, the mirror lags it,
    so the pixel markers start early (or a long cable makes them late)
  * to measure the shift from the images (``helpers.imageShift``): the
    FLIM intensity against the truth -- the simulated sample here, the APD
    image on a rig -- and to turn pixels into picoseconds
  * ``lineClockDelayPs``: positive when the clock is early (the detector
    delays its pixel pattern), negative when it is late (the card moves the
    timestamps earlier), and that a re-run shows the shift at 0

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a line clock firing two pixels early (a mock fault this
                script injects and removes; nothing on a real card), so the
                first run is shifted and the second, with the delay set, is
                not.
  Your own microscope: the APD and the FLIM detector image the same scan;
                set TRUTH_DETECTOR to your point detector. Two FLIM runs of
                the 64 x 64 scan; a scan with structure along x (beads, a
                grid) measures the shift best.

Next: 10_flim_preflight.py
"""

import os

from imswitch.imcontrol.model.timeresolved import TimeResolvedScanConfig

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
TRUTH_DETECTOR = 'APD'
APPLY = False
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')

if DETECTOR is None:
    raise RuntimeError('This setup has no FLIM detector to align.')

facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
tr = facade.time_resolved
if tr is None:
    raise RuntimeError('The workflow facade found no time-resolved detector.')

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, True)
dwell_ps = design['dwell_s'] * 1e12
delay0 = tt.channels()['line_clock'].delay_ps
# On the mock: the line clock fires two pixels early (a galvo lagging its
# command). On a real card this call does nothing and the rig's own skew is
# what the first run measures.
tt.set_mock_fault('line_delay_ps', -2 * dwell_ps)


def flimImage(label):
    """Run the scan with FLIM in it; return its final intensity image."""
    token = tr.configure(TimeResolvedScanConfig(), owner=f'tutorial09-{label}')
    try:
        runScanAndWait(timeout=120)
        products = tr.wait_for_final(timeout_s=30, owner=token)
        return products.intensity
    finally:
        tr.clear(token)


def truthFor(image):
    """What the FLIM image should look like: the mock sample on the mock,
    the APD's image of the same scan on a rig."""
    truth = tt.mock_truth(*image.shape)
    if truth is not None:
        return truth[0]
    apd = api.imcontrol.getDetectorLatestFrame(TRUTH_DETECTOR)
    if apd.shape != image.shape:
        raise RuntimeError(f'{TRUTH_DETECTOR} image is {apd.shape}, FLIM is {image.shape}: '
                           'both detectors must take part in the same scan.')
    return apd


try:
    image = flimImage('first')
    shift, match = helpers.imageShift(image, truthFor(image))
    print(f'first run: the FLIM image is the truth shifted by {shift:+d} px along x '
          f'(match {match:.2f})')
    # measured[:, k] ~ truth[:, k + shift]: the markers are `shift` pixels
    # late (negative: early). The delay cancels that lateness.
    lateness_ps = shift * dwell_ps
    new_delay = int(round(delay0 - lateness_ps))
    print(f'  -> the pixel markers are {lateness_ps / 1e6:+.3f} ms '
          f'({shift:+d} x {dwell_ps / 1e6:.3f} ms dwell) late against the beam.')
    print(f'  -> lineClockDelayPs = {new_delay} (now {delay0}): '
          + ('positive, so the detector delays its pixel pattern by it'
             if new_delay > 0 else
             'negative, so the card moves the line timestamps earlier by it'
             if new_delay < 0 else 'nothing to correct'))
    print()

    if shift != 0:
        tt.set_delay('line_clock', new_delay)         # the scan is over: not held
        image = flimImage('second')
        shift2, match2 = helpers.imageShift(image, truthFor(image))
        print(f'second run, with the delay: shifted by {shift2:+d} px (match {match2:.2f})')
        print('  -> ' + ('aligned.' if shift2 == 0 else
                         'still off: the shift was not an integer number of pixels, or the'
                         ' truth image differs; refine from here.'))
        print()

    if APPLY and shift != 0:
        print(f'Applied: lineClockDelayPs = {new_delay} until restart; keep it with')
        print(f'  "lineClockDelayPs": {new_delay}  in the timeTagger block.')
    else:
        if shift != 0:
            tt.set_delay('line_clock', delay0)
        print(f'Not applied (APPLY is False): lineClockDelayPs stays {delay0}.')
finally:
    tt.set_mock_fault('line_delay_ps', None)
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
