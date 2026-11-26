"""Driver for test_smartpipeline_triggered_scan_saved (run in a subprocess).

Boots the real ImControl offscreen on ``simple_mock_setup`` with the
Advanced scan panel (no SimplePointScan cloak) and the Smart Pipeline
panel, then:

* ``adapter``: the ``run-scan`` action's real adapter, with the run's
  recorder, scans two ROI boxes one after the other on the real Advanced
  scan controller (simulated NI-DAQ) -- each scan must be saved as its own
  scan-once recording carrying the event's metadata; then a recording that
  the Recording controller refuses must fail the action (nothing scanned
  unsaved);
* ``arm``: the Smart Pipeline panel arms a pipeline in Experiment with an
  unusable recordings folder (refused, nothing armed) and in Validate (armed,
  with a note).

Prints ``RESULT <json>``.
"""
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


def _boot():
    import dataclasses

    from imswitch import imcontrol
    import imswitch.improcess as improcess
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcontrol.view import ViewSetupInfo
    from imswitch.imcontrol._test import optionsBasic

    setup = driver.loadSetup('simple_mock_setup')
    setup['scan'].pop('cloak', None)
    setup['availableWidgets'] = [w for w in setup['availableWidgets'] if w != 'Image'] + ['SmartPipeline']
    setupInfo = ViewSetupInfo.from_json(json.dumps(setup), infer_missing=True)
    options = dataclasses.replace(optionsBasic, setupFileName='simple_mock_setup.json')
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    channel.register(improcess)
    _, controller = imcontrol.getMainViewAndController(channel, overrideSetupInfo=setupInfo,
                                                      overrideOptions=options)
    return controller


def _pump(seconds):
    QtTest.QTest.qWait(int(seconds * 1000))


def _request(label, box_um, dff=0.8, tick=7):
    from imswitch.improcess.model.points_result import PointsResult
    from imswitch.imcontrol.model.smartpipeline.catalog import RUN_SCAN
    from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest

    y0, x0, y1, x1 = box_um
    points = PointsResult('trigger', np.array([[(y0 + y1) / 2, (x0 + x1) / 2]]), frame='scanner:galvo',
                          axis_names=['y', 'x'], properties={
        'label': np.array([label]), 'dff': np.array([dff]), 'F': np.array([180.0]), 'F0': np.array([100.0]),
        'area': np.array([64]),
        'src_bbox_y0': np.array([10.5]), 'src_bbox_x0': np.array([20.5]),
        'src_bbox_y1': np.array([18.5]), 'src_bbox_x1': np.array([28.5]),
        'bbox_y0': np.array([y0]), 'bbox_x0': np.array([x0]), 'bbox_y1': np.array([y1]), 'bbox_x1': np.array([x1]),
    }, metadata={'transform': {'id': 'cam_to_scan', 'type': 'affine', 'residual_rms_um': 0.031}})
    params = {**RUN_SCAN.params, 'source': 'Scan', 'axes': ['Y', 'X'], 'frame': 'scanner:galvo', 'points': 'first',
              'lasers': ['488 (EXC)'], 'margin_um': 0.5, 'min_size_um': 2.0, 'max_size_um': 10.0, 'pixel_um': 0.2}
    return ActionRequest(step_id='scan', action='run-scan', params=params, inputs=[points], mode='experiment',
                         tick=tick, epoch=1, timeout_s=60,
                         context={'frame_sequence': tick - 1, 'frame_wall': time.time() - 0.05,
                                  'decision_wall': time.time()})


def _wait(completion, seconds=60):
    deadline = time.monotonic() + seconds
    while not completion.done and time.monotonic() < deadline:
        _pump(0.05)
    return completion.done


def adapter_scenario(controller, out):
    from imswitch.imcommon.model.api import _UIThreadExecWrapper
    from imswitch.imcontrol.model.smartpipeline.event_recording import (
        TriggeredScanRecorder, scan_driven_detectors,
    )
    from imswitch.imcontrol.model.smartpipeline.live_actions import RunScanAdapter

    master = controller._ImConMainController__masterController
    comm = controller._ImConMainController__commChannel
    lasers = controller.controllers['Laser']
    lasers.setLaserValue('488 (EXC)', 1.0)
    lasers.setLaserActive('488 (EXC)', True)
    marshal = _UIThreadExecWrapper(lambda fn: fn())
    folder = os.path.join(out, 'recordings', 'smart_pipeline', 'run1')
    recorder = TriggeredScanRecorder(comm.controllerRegistry, marshal, folder=folder,
                                     detectors=scan_driven_detectors(master.detectorsManager),
                                     base_attrs={'SmartPipeline:pipeline': 'driver'},
                                     stage_position=lambda: {'Stage': {'X': 12.5, 'Y': -3.0}})
    report = {'detectors': list(recorder.detectors), 'problems': recorder.problems('Scan')}
    scan = RunScanAdapter(comm.controllerRegistry, comm.scanWorkflow, marshal=marshal, recorder=recorder)
    apd = master.detectorsManager['APD']
    events = []
    for label, box in ((3, (-4.0, -6.0, 0.0, -2.0)), (7, (1.0, 2.0, 5.0, 6.0))):
        t0 = time.perf_counter()
        completion = scan.start(_request(label, box), None)
        finished = _wait(completion)
        latest = apd.getLatestFrameShared(is_save=False)
        events.append({
            'finished': finished, 'outcome': completion.outcome, 'error': completion.error,
            'result': json.loads(json.dumps(completion.result, default=str)) if completion.result else None,
            'seconds': time.perf_counter() - t0,
            'latest': None if latest is None else np.asarray(latest).tolist(),
        })
        _pump(0.3)
    report['events'] = events

    # The same scan without saving: what the recording adds before the scan starts.
    bare = RunScanAdapter(comm.controllerRegistry, comm.scanWorkflow, marshal=marshal)
    completion = bare.start(_request(9, (-4.0, -6.0, 0.0, -2.0)), None)
    _wait(completion)
    report['bare'] = json.loads(json.dumps(completion.result, default=str)) if completion.result else None
    _pump(0.3)

    # A recording the Recording controller refuses: the action fails, nothing is scanned unsaved.
    recordingController = controller.controllers['Recording']
    original = recordingController.recordScanSeries
    started = []
    comm.sigScanStarted.connect(lambda: started.append(time.monotonic()))

    def refuse(*args, **kwargs):
        from imswitch.imcontrol.controller.controllers.RecordingController import ScanSeriesRecordingResult
        return ScanSeriesRecordingResult(False, 'disk full (simulated)')

    recordingController.recordScanSeries = refuse
    try:
        completion = scan.start(_request(11, (-4.0, -6.0, 0.0, -2.0)), None)
        _wait(completion)
        _pump(0.5)
    finally:
        recordingController.recordScanSeries = original
    report['refused'] = {'outcome': completion.outcome, 'error': completion.error, 'scans_started': len(started)}
    return report


def arm_scenario(controller, out):
    from imswitch.imcommon.model import dirtools

    sp = controller.controllers['SmartPipeline']
    sp._applyMode = lambda name: None
    rec = controller.controllers['Recording']
    blocker = os.path.join(out, 'not-a-folder')
    with open(blocker, 'w') as handle:
        handle.write('x')
    rec.setRecFolder(os.path.join(blocker, 'data'))
    doc = os.path.join(dirtools.UserFileDirs.Root, 'smart_pipelines', 'roi_dff_triggered_apd_scan.json')
    calibrations = os.path.join(dirtools.UserFileDirs.Root, 'calibrations')
    os.makedirs(calibrations, exist_ok=True)
    from imswitch.imcommon.model.smartpipeline import ScaleFlip
    with open(os.path.join(calibrations, 'cam_to_scan.json'), 'w') as handle:
        json.dump(ScaleFlip('cam_to_scan', 'detector:Camera', 'scanner:galvo', pixel_size=(0.1, 0.1),
                            origin=(400, 400)).to_dict(), handle)
    sp.setPipelinePath(doc)
    report = {'load_issues': list(sp._issues)}
    sp._widget.setMode('experiment')
    sp.arm()
    report['experiment'] = {'armed': sp._run is not None, 'issues': sp._widget.issuesLabel.text(),
                            'status': sp._widget.statusLabel.text()}
    sp._widget.setMode('validate')
    sp.arm()
    report['validate'] = {'armed': sp._run is not None, 'issues': sp._widget.issuesLabel.text()}
    sp.stop()
    _pump(1.0)
    return report


def main(out):
    controller = _boot()
    _pump(0.5)
    result = {'adapter': adapter_scenario(controller, out), 'arm': arm_scenario(controller, out)}
    print('RESULT ' + json.dumps(result, default=str))


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except BaseException:
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    os._exit(0)
