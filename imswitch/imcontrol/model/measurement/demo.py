"""Write a mock polarisation-map run file, for trying the ImProcess side.

    python -m imswitch.imcontrol.model.measurement.demo OUTPUT_FOLDER
        [--hwp-step 2.5] [--qwp-step 5]

Two mock waveplates (quarter-wave then half-wave, with small mount offsets)
in front of a mock PAX1000 are stepped over a grid. The resulting
``*.run.h5`` opens in ImProcess with the *Polarisation map* reconstructor.
No hardware is touched.

Grid density matters: a half-wave plate turned by θ rotates the state by 4θ
on the Poincaré sphere (a quarter-wave plate by about 2θ), so the HWP needs
the finer step, and 0–90° covers it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from imswitch.imcommon.algorithms.polarisation import TwoPlateModel

from .generators import grid
from .instrument import InstrumentSession
from .mocks import MockPAXDriver, MockRotatorControl
from .runner import MeasurementRunner, RunReport, RunSettings

#: Quarter-wave then half-wave plate, with small mount offsets.
DEMO_MODEL = TwoPlateModel(offset1_deg=2.0, offset2_deg=-1.5)


def run_demo(folder: Path, *, hwp_step_deg: float = 2.5, qwp_step_deg: float = 5.0,
             samples: int = 2, noise_deg: float = 0.3, progress=None) -> RunReport:
    qwp = MockRotatorControl('qwp', speed_deg_s=5000.0)
    hwp = MockRotatorControl('hwp', speed_deg_s=5000.0)
    driver = MockPAXDriver(qwp, hwp, model=DEMO_MODEL,
                           revolution_s=0.005, noise_deg=noise_deg)
    session = InstrumentSession('pax1', driver)
    session.connect()
    sequence = grid([('hwp', list(np.arange(0.0, 90.0, hwp_step_deg))),
                     ('qwp', list(np.arange(0.0, 180.0, qwp_step_deg)))])
    settings = RunSettings(
        samples_per_point=samples, plane_label='mock sample plane',
        illumination={'source': 'mock 633 nm', 'wavelength_nm': 633.0},
        notes='Mock run written by imswitch.imcontrol.model.measurement.demo',
    )
    runner = MeasurementRunner(sequence=sequence, controls=[hwp, qwp],
                               instruments={'pax1': session}, folder=folder,
                               settings=settings, progress=progress)
    try:
        return runner.run()
    finally:
        session.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('folder', type=Path)
    parser.add_argument('--hwp-step', type=float, default=2.5, help='HWP step, degrees (0-90)')
    parser.add_argument('--qwp-step', type=float, default=5.0, help='QWP step, degrees (0-180)')
    parser.add_argument('--samples', type=int, default=2, help='samples per point')
    args = parser.parse_args(argv)

    def progress(event):
        if event.point % 50 == 0 or event.point == event.total - 1:
            print(f'point {event.point + 1}/{event.total}: {event.status.value}')

    report = run_demo(args.folder, hwp_step_deg=args.hwp_step,
                      qwp_step_deg=args.qwp_step, samples=args.samples,
                      progress=progress)
    print(f'acquisition {report.acquisition.value}, cleanup {report.cleanup.value}, '
          f'{report.points_committed}/{report.points_total} points committed')
    print(f'run file: {report.run_file}')
    return 0 if report.run_file else 1


if __name__ == '__main__':
    raise SystemExit(main())
