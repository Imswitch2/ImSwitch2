"""Tutorial measurement 01 -- Map the polarisation of two waveplates.

You will learn
  * instruments: ``getInstruments()``, ``connectInstrument()``,
    ``readInstrument()`` -- a polarimeter that is plugged in when needed
  * a measurement run: ``measureRotatorGrid()`` steps two rotators over a
    grid and samples the polarimeter after every move, into one run file
  * to view the result in ImProcess as a map and on the Poincaré sphere

Setup
  Mock setup:   example_no_hardware.json
  It simulates: two waveplates in rotation mounts ("Mock QWP", "Mock HWP")
                in front of a polarimeter ("Mock PAX"), which reads the
                polarisation those two plates make. The polarimeter starts
                not connected, like a real one you plug in for the
                measurement.
  Your own microscope: two rotators holding a quarter-wave and a half-wave
                plate (for example Standa, Kinesis or Elliptec mounts) and a
                PAX1000 in the setup's "instruments" section. Change QWP,
                HWP and PAX to their names, SIMULATED to False, and the
                angle steps to the finer ones in the comments.

Next: open the run file in ImProcess (the steps are printed at the end).

Viewing the result in ImProcess
  1. Tools > Load reconstructor > Polarisation map (once per session).
  2. File > Open the printed *.run.h5 file.
  3. Choose "Polarisation map" in the Parameters panel and run it.
  You get: a map of the measured polarisation over the two angles (azimuth,
  ellipticity, degree of polarisation), the measured states as points on a
  3D Poincaré sphere, and a table of the best angle pair for each target
  polarisation (linear H/V/±45°, circular L/R).
"""

QWP = 'Mock QWP'
HWP = 'Mock HWP'
PAX = 'Mock PAX'
# A half-wave plate turned by θ turns the polarisation by 4θ on the Poincaré
# sphere, a quarter-wave plate by about 2θ: the HWP needs the finer step,
# and 0-90° covers it. These steps keep the tutorial short; for a real
# calibration use 2.5° (HWP) and 5° (QWP).
HWP_ANGLES = [i * 7.5 for i in range(13)]     # 0 ... 90°
QWP_ANGLES = [i * 15.0 for i in range(13)]    # 0 ... 180°
SAMPLES_PER_POINT = 3
# The mock rotators are simulations; a real run refuses simulated devices
# (a mount that fell back to a simulation would only pretend to move).
SIMULATED = True
# Until the PAX1000's timing has been checked on hardware, its samples are
# "unverified": the run is kept and can be viewed, but marked as such.
ALLOW_UNVERIFIED_TIMING = True

print('Instruments:', [(i['name'], 'connected' if i['connected'] else 'not connected')
                       for i in api.imcontrol.getInstruments()])

# Connect the polarimeter if it is not -- and disconnect it again at the end,
# so the setup is left as it was.
wasConnected = next(i['connected'] for i in api.imcontrol.getInstruments()
                    if i['name'] == PAX)
if not wasConnected:
    api.imcontrol.connectInstrument(PAX)
try:
    reading = api.imcontrol.readInstrument(PAX)[0]
    print(f'{PAX} now: azimuth {reading["azimuth"]:.3f} rad, '
          f'ellipticity {reading["ellipticity"]:.3f} rad, DOP {reading["dop"]:.3f}')

    total = len(HWP_ANGLES) * len(QWP_ANGLES)

    def progress(event):
        if event.point % 20 == 0 or event.point + 1 == event.total:
            print(f'point {event.point + 1}/{event.total}: {event.status.value}')

    # The rotators, the polarimeter and the waveform outputs are reserved for
    # the whole run: nobody else can move or reconfigure them meanwhile. The
    # QWP is the outer axis, the HWP the inner one; 'snake' reverses the
    # inner axis on every other line, so the HWP never travels back.
    print(f'Measuring {total} points...')
    report = api.imcontrol.measureRotatorGrid(
        [(QWP, QWP_ANGLES), (HWP, HWP_ANGLES)], [PAX],
        samples_per_point=SAMPLES_PER_POINT, settle_s=0.1,
        allow_simulated=SIMULATED,
        allow_unverified_timing=ALLOW_UNVERIFIED_TIMING,
        notes='tutorial measurement 01', progress=progress,
    )
finally:
    if not wasConnected:
        api.imcontrol.disconnectInstrument(PAX)

print(f'acquisition {report.acquisition.value}: {report.points_committed} of '
      f'{report.points_total} points; cleanup {report.cleanup.value}')
if report.run_file is None:
    raise RuntimeError(f'the run wrote no file: {report.detail}')
print(f'Run file: {report.run_file}')
print('In ImProcess: Tools > Load reconstructor > Polarisation map, then '
      'File > Open the run file, and run "Polarisation map" from the '
      'Parameters panel.')
