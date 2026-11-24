"""Driver for test_replay_end_to_end (run in a subprocess, see there).

Boots the real ImControl offscreen on the shipped ``simple_mock_setup``,
records a snapshot (HDF5 and Zarr), then boots other setups -- a variant of
the same file with renamed devices and a narrower laser range, the shipped
``mock_scan_setup`` and ``example_mock`` -- and loads the recording into
each through the real File-menu handlers, with every hardware manager
wrapped in a spy.  The image widgets are left out: they need a GPU-backed
napari viewer that does not start headless.

Prints one JSON document with what happened.
"""
import dataclasses
import functools
import glob
import json
import os
import sys
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('MPLBACKEND', 'Agg')

import imswitch  # noqa: E402
from imswitch import imcontrol  # noqa: E402
from imswitch.imcommon import prepareApp  # noqa: E402
from imswitch.imcommon.controller import ModuleCommunicationChannel  # noqa: E402
from imswitch.imcontrol.view import ViewSetupInfo  # noqa: E402
from imswitch.imcontrol._test import optionsBasic  # noqa: E402
from qtpy import QtTest  # noqa: E402

SETUPS = os.path.join(os.path.dirname(imswitch.__file__), '_data', 'user_defaults',
                      'imcontrol_setups')
SPIED = ('setValue', 'setEnabled', 'setPosition', 'move', 'move_abs', 'move_rel',
         'setParameter', 'setBinning', 'crop', 'setModulationEnabled', 'move_to')

app = prepareApp()
_excepthookCalls = []
sys.excepthook = lambda *exc: _excepthookCalls.append(''.join(traceback.format_exception(*exc)))


def loadSetup(name):
    with open(os.path.join(SETUPS, name + '.json')) as f:
        return json.load(f)


def boot(data, fileName):
    data = dict(data)
    data['availableWidgets'] = [w for w in data.get('availableWidgets', []) if w != 'Image']
    setupInfo = ViewSetupInfo.from_json(json.dumps(data), infer_missing=True)
    options = dataclasses.replace(optionsBasic, setupFileName=fileName)
    channel = ModuleCommunicationChannel()
    channel.register(imcontrol)
    view, controller = imcontrol.getMainViewAndController(
        channel, overrideSetupInfo=setupInfo, overrideOptions=options)
    return view, controller


def installSpies(controller, log):
    master = controller._ImConMainController__masterController
    for attr in ('lasersManager', 'positionersManager', 'detectorsManager',
                 'rotatorsManager', 'flipMirrorsManager'):
        manager = getattr(master, attr, None)
        if manager is None:
            continue
        for name, device in manager:
            for method in SPIED:
                original = getattr(device, method, None)
                if not callable(original):
                    continue

                def spy(*args, __o=original, __n=name, __m=method, **kwargs):
                    log.append([__n, __m, [repr(a) for a in args]])
                    return __o(*args, **kwargs)
                functools.update_wrapper(spy, original)
                setattr(device, method, spy)


def snap(controller, folder, fmt):
    rec = controller.controllers['Recording']
    rec.setRecFolder(folder)
    rec.setRecFileFormat(fmt)
    rec._widget.setSaveSnapFormat(fmt)
    rec.snap()
    pattern = '*.h5' if fmt == 'HDF5' else '*.zarr'
    for _ in range(100):
        QtTest.QTest.qWait(50)
        found = [p for p in glob.glob(os.path.join(folder, pattern)) if not p.endswith('.tmp')]
        if found:
            return found[0]
    raise RuntimeError(f'no {fmt} snapshot in {folder}')


def replayInto(controller, path, isZarr, decide=None):
    import imswitch.imcontrol.controller.ImConMainController as main
    log = []
    installSpies(controller, log)
    seen = {}
    dialog = controller.replayController._widget

    def askToApply(plan, canRemember=False):
        seen['callsAtAsk'] = list(log)
        seen['items'] = {i.id: [i.status, i.target] for i in plan.items}
        if decide:
            decide(plan)
        return True

    def showResult(result):
        seen['result'] = result.toDict()

    dialog.askToApply, dialog.showResult = askToApply, showResult
    dialog.rememberRequested = lambda: False
    originalFile, originalFolder = main.guitools.askForFilePath, main.guitools.askForFolderPath
    main.guitools.askForFilePath = lambda *a, **k: path
    main.guitools.askForFolderPath = lambda *a, **k: path
    error = None
    try:
        (controller.loadParamsFromZarr if isZarr else controller.loadParamsFromHDF5)()
    except Exception:  # noqa: BLE001 - reported to the test
        error = traceback.format_exc()
    finally:
        main.guitools.askForFilePath, main.guitools.askForFolderPath = originalFile, originalFolder
    seen['error'] = error
    seen['callsAfter'] = list(log)
    return seen


def main(outDir):
    report = {}
    source = loadSetup('simple_mock_setup')
    # Give two of the (simulated) NI-DAQ lasers an analog line so they have a
    # power value to replay; the shipped ones are on/off only.
    source['lasers']['488 (EXC)']['analogChannel'] = 'Dev1/ao2'
    source['lasers']['561 (EXC)']['analogChannel'] = 'Dev1/ao3'
    viewA, a = boot(source, 'simple_mock_setup.json')
    a.controllers['Laser'].setLaserValue('488 (EXC)', 42.0)
    a.controllers['Laser'].setLaserValue('561 (EXC)', 150.0)
    a.controllers['Laser'].setLaserActive('405 (ON)', True)
    a.controllers['Positioner'].setPositioner('Z', 'Z', 3.0)
    h5 = snap(a, os.path.join(outDir, 'a_h5'), 'HDF5')
    zr = snap(a, os.path.join(outDir, 'a_zarr'), 'ZARR')
    a.controllers['Laser'].setLaserActive('405 (ON)', False)
    report['source'] = {'h5': h5, 'zarr': zr, 'setup': source}

    variant = json.loads(json.dumps(source))
    variant['lasers']['Ex488'] = variant['lasers'].pop('488 (EXC)')
    variant['lasers']['561 (EXC)']['valueRangeMax'] = 100
    variant['positioners']['Focus'] = variant['positioners'].pop('Z')
    for key in ('scan',):
        variant.pop(key, None)
    variant['availableWidgets'] = [w for w in variant['availableWidgets'] if w != 'Scan']
    targets = [('variant', variant, 'variant.json'),
               ('mock_scan_setup', loadSetup('mock_scan_setup'), 'mock_scan_setup.json'),
               ('example_mock', loadSetup('example_mock'), 'example_mock.json')]
    for label, data, fileName in targets:
        _, controller = boot(data, fileName)
        report[label] = {'hdf5': replayInto(controller, h5, False),
                         'zarr': replayInto(controller, zr, True)}
        if label == 'variant':
            # Moves opted in: the remapped Z axis may move, the laser still
            # never switches on.
            def optIn(plan):
                plan.allowMoves = True
                plan.retarget('positioner:Z:Z', 'Focus:Z')
            report[label]['moves'] = replayInto(controller, h5, False, decide=optIn)
            after = snap(controller, os.path.join(outDir, 'variant_h5'), 'HDF5')
            from imswitch.imcontrol.controller.controllers.ReplayController import (
                readHDF5Attributes)
            attrs, _, _ = readHDF5Attributes(after)
            report[label]['afterProvenance'] = {
                ':'.join(k): v for k, v in attrs.items() if k[0] == 'replay'}
    attrsA, _, _ = __import__(
        'imswitch.imcontrol.controller.controllers.ReplayController',
        fromlist=['readHDF5Attributes']).readHDF5Attributes(h5)
    report['sourceAttrs'] = json.loads(json.dumps(
        {':'.join(k): v for k, v in attrsA.items()}, default=str))
    report['excepthook'] = _excepthookCalls
    print('REPORT ' + json.dumps(report, default=str))


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except BaseException:
        sys.stderr.write(traceback.format_exc())
        raise
