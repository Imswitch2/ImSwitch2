"""Driver for test_etsted_pipeline_smartpipeline_e2e (run in a subprocess).

Boots the real ImControl offscreen on the shipped ``etsted_advanced_mock_setup`` (a widefield mock camera, an
APD, the simulated NI-DAQ and the Advanced scan panel) with the Smart Pipeline panel, and:

* the user folder is as the first start leaves it: ``imcontrol_etsted/analysis_pipelines/dff_event.py`` and
  ``smart_pipelines/etsted_dff_event.json`` came with the shipped defaults;
* the shipped document is loaded in the panel (no issues, its controls listed);
* it is armed in **Validate** on a camera whose frames show a bright spot once switched on: the dry run must
  say it WOULD scan at the spot (through the camera -> scan calibration), start no scan, and keep watching;
* a control (``dff_threshold``) is edited while armed and reaches the pipeline step.

Prints ``RESULT <json>``.
"""
import dataclasses
import json
import os
import sys
import time
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('MPLBACKEND', 'Agg')

import numpy as np  # noqa: E402

from imswitch.imcontrol._test.unit import _replay_e2e_driver as driver  # noqa: E402
from qtpy import QtTest  # noqa: E402

#: The spot the camera shows once on: row, column of the 512 x 512 frame.
SPOT = (200, 300)


def _pump(seconds):
    QtTest.QTest.qWait(int(seconds * 1000))


def _boot():
    from imswitch import imcontrol
    import imswitch.improcess as improcess
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcontrol.view import ViewSetupInfo
    from imswitch.imcontrol._test import optionsBasic

    setup = driver.loadSetup('etsted_advanced_mock_setup')
    setup['availableWidgets'] = [w for w in setup['availableWidgets'] if w not in ('Image', 'EtSTED')] + ['SmartPipeline']
    setupInfo = ViewSetupInfo.from_json(json.dumps(setup), infer_missing=True)
    options = dataclasses.replace(optionsBasic, setupFileName='etsted_advanced_mock_setup.json')
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    channel.register(improcess)
    _, controller = imcontrol.getMainViewAndController(channel, overrideSetupInfo=setupInfo, overrideOptions=options)
    return controller


def _spot_source(controller):
    """Replace the mock camera's frames: a quiet noisy sample, plus a Gaussian spot while ``event.on``."""
    master = controller._ImConMainController__masterController
    source = master.detectorsManager['Camera']._camera.mock_frame_source
    rng = np.random.default_rng(3)
    state = {'on': False, 'frames': 0}

    def live(shape):
        state['frames'] += 1
        frame = rng.poisson(300.0, shape).astype(np.float32)
        if state['on']:
            yy, xx = np.mgrid[:shape[0], :shape[1]]
            frame += 1500.0 * np.exp(-((yy - SPOT[0]) ** 2 + (xx - SPOT[1]) ** 2) / (2 * 6.0 ** 2))
        return np.clip(frame, 0, 65535).astype(np.uint16)

    source.liveFrame = live
    return state


def validate_scenario(controller, out):
    from imswitch.imcommon.model import dirtools
    from imswitch.imcommon.model.smartpipeline import ScaleFlip

    sp = controller.controllers['SmartPipeline']
    sp._applyMode = lambda name: None
    spot = _spot_source(controller)
    controller.controllers['Laser'].setLaserValue('488 (EXC)', 1.0)
    controller.controllers['Laser'].setLaserActive('488 (EXC)', True)
    root = dirtools.UserFileDirs.Root
    report = {'defaults_synced': {
        'pipeline': os.path.isfile(os.path.join(root, 'imcontrol_etsted', 'analysis_pipelines', 'dff_event.py')),
        'document': os.path.isfile(os.path.join(root, 'smart_pipelines', 'etsted_dff_event.json'))}}
    # a camera -> scan calibration for this setup (0.05 um camera pixels, the frame's centre is the scan's origin)
    os.makedirs(os.path.join(root, 'calibrations'), exist_ok=True)
    with open(os.path.join(root, 'calibrations', 'cam_to_scan.json'), 'w') as handle:
        json.dump(ScaleFlip('cam_to_scan', 'detector:Camera', 'scanner:galvo', pixel_size=(0.05, 0.05),
                            origin=(256, 256)).to_dict(), handle)
    sp.setPipelinePath(os.path.join(root, 'smart_pipelines', 'etsted_dff_event.json'))
    report['load_issues'] = list(sp._issues)
    report['controls'] = sorted(sp.parameterValues())
    report['control_help'] = {p.name: p.help for p in sp._parameters}.get('dff_threshold', '')
    sp.setParameter('warmup_frames', 3)
    sp.setParameter('cooldown_s', 0.5)
    sp._widget.setMode('validate')
    sp.arm()
    run = sp._run
    report['armed'] = run is not None and run.state in ('armed', 'running')
    report['arm_issues'] = sp._widget.issuesLabel.text()
    if not report['armed']:
        return report
    report['preflight'] = sp._widget.getPreflightText()
    engine = run.engine
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and engine.summary.ticks < 14:
        _pump(0.1)
    report['ticks_before_event'] = engine.summary.ticks
    report['would_before_event'] = len(engine.log.events('would', step='scan'))
    # a control edited while armed reaches the step
    report['set_threshold'] = sp.setParameter('dff_threshold', 0.4)
    step = next(s for s in engine.workflow.steps if s.id == 'etsted')
    report['threshold_in_step'] = step.params.get('dff_threshold')
    report['status_before'] = sp._widget.statusLabel.text()
    spot['on'] = True
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not engine.log.events('would', step='scan'):
        _pump(0.1)
    spot['on'] = False
    would = engine.log.events('would', step='scan')
    report['would'] = [{'tick': e.tick, 'message': e.detail.get('message'),
                        'inputs': json.loads(json.dumps(e.detail.get('inputs'), default=str))} for e in would[:2]]
    report['scans_started'] = engine.summary.actions_started
    report['started_events'] = len(engine.log.events('started', step='scan'))
    report['summary_state'] = {'ticks': engine.summary.ticks, 'error': engine.summary.error}
    report['log_tail'] = sp._widget.logView.toPlainText().splitlines()[-12:]
    report['status_after'] = sp._widget.statusLabel.text()
    report['spot_frames'] = spot['frames']
    sp.stop()
    _pump(1.0)
    report['run_state_after_stop'] = None if sp._run is None else sp._run.state
    return report


def main(out):
    controller = _boot()
    _pump(0.5)
    print('RESULT ' + json.dumps({'validate': validate_scenario(controller, out)}, default=str))


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except BaseException:
        sys.stderr.write(traceback.format_exc())
