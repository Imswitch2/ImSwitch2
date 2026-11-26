"""A whole-application rig for the etSTED panel: the REAL main controller on a
shipped mock setup (simulated NI-DAQ, Advanced scan panel, mock camera and
APD), with the panel's user files in a throwaway directory.

The older etSTED tests drive the controller with mock scan managers; this rig
is what proves the triggered scan against the real scan controller, designers
and NI-DAQ manager.
"""
import json
import shutil
import time
from pathlib import Path

import numpy as np

SETUP_DIR = Path(__file__).resolve().parents[3] / '_data' / 'user_defaults' / 'imcontrol_setups'
ETSTED_ADVANCED_SETUP = SETUP_DIR / 'mock' / 'point_scanning' / 'etsted_advanced_mock_setup.json'

SHIPPED_ETSTED = SETUP_DIR.parent / 'imcontrol_etsted'

class EtSTEDRig:
    """Main controller + etSTED panel on a mock setup, in ``root``."""

    def __init__(self, root, setup_path=ETSTED_ADVANCED_SETUP, pipelines=None,
                 coefficients=None, setup_edit=None):
        from imswitch.test_no_hardware_ui_smoke import _install_gui_dependency_stubs
        _install_gui_dependency_stubs()
        from imswitch import imcontrol
        from imswitch.imcommon import prepareApp
        from imswitch.imcommon.controller import ModuleCommunicationChannel
        from imswitch.imcommon.model import dirtools
        from imswitch.imcommon.view.guitools import naparitools
        from imswitch.imcontrol.controller.ImConMainController import ImConMainController
        from imswitch.imcontrol.model import Options
        from imswitch.imcontrol.view import ViewSetupInfo
        root = Path(root)
        self._saved = (dirtools._baseUserFilesDir, dirtools.UserFileDirs.Root,
                       dirtools.UserFileDirs.Config,
                       ImConMainController._shouldSaveWidgetStateOnClose,
                       getattr(naparitools.EmbeddedNapari, 'add_points', None))
        dirtools._baseUserFilesDir = root
        dirtools.UserFileDirs.Root = str(root)
        dirtools.UserFileDirs.Config = str(root / 'config')
        ImConMainController._shouldSaveWidgetStateOnClose = lambda self: False

        class _Layer:
            data = []
            metadata = {}

        naparitools.EmbeddedNapari.add_points = lambda self, *a, **k: _Layer()
        # The stubbed napari layers carry no ``metadata``, which the image
        # widget reads to draw a scan frame's geometry overlay.
        from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget
        self._image_widget = ImageWidget
        self._saved_geometry = ImageWidget._setScanGeometry
        ImageWidget._setScanGeometry = lambda self, *a, **k: None
        self._dirtools, self._main_cls, self._napari = dirtools, ImConMainController, naparitools

        # What the user-defaults sync puts in the user folder on first start.
        etsted_dir = root / 'imcontrol_etsted'
        shutil.copytree(SHIPPED_ETSTED, etsted_dir)
        for name, source in (pipelines or {}).items():
            (etsted_dir / 'analysis_pipelines' / f'{name}.py').write_text(source)
        if coefficients is not None:
            (etsted_dir / 'transform_pipelines' / 'rig.csv').write_text(
                '\n'.join(repr(float(x)) for x in coefficients) + '\n')

        setup = json.loads(Path(setup_path).read_text())
        if setup_edit is not None:
            setup_edit(setup)
        self.setup_dict = setup
        info = ViewSetupInfo.from_json(json.dumps(setup), infer_missing=True)
        channel = ModuleCommunicationChannel()
        channel.register(imcontrol)
        self.app = prepareApp()
        self.view, self.main = imcontrol.getMainViewAndController(
            channel, overrideSetupInfo=info,
            overrideOptions=Options(setupFileName='etsted_rig.json'))
        self.process()
        self.etsted = self.main.controllers['EtSTED']
        self.widget = self.etsted._widget
        self.scan = self.main.controllers['Scan']
        self.master = self.etsted._master
        self.channel = self.etsted._commChannel

    def process(self, seconds=0.0):
        end = time.monotonic() + seconds
        self.app.processEvents()
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def wait_for(self, predicate, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.process()
            if predicate():
                return True
            time.sleep(0.01)
        self.process()
        return bool(predicate())

    def switch_laser_on(self, name='488 (EXC)', fraction=0.5):
        """The Laser panel's switch: what the mock camera and APD see."""
        laser = self.main.controllers['Laser']
        laser.toggleLaser(name, True)
        laser.valueChanged(name, fraction * self.master.lasersManager[name].valueRangeMax)

    def event_source(self, col=300, row=200, amplitude=1500.0, sigma_px=6.0):
        """Make the mock camera show a bright spot once ``on`` is set."""
        from types import SimpleNamespace
        source = self.master.detectorsManager['Camera']._camera.mock_frame_source
        original = source.liveFrame
        event = SimpleNamespace(on=False, col=col, row=row)

        def live(shape):
            frame = original(shape).astype(np.float32)
            if event.on:
                yy, xx = np.mgrid[:shape[0], :shape[1]]
                frame += amplitude * np.exp(
                    -((yy - event.row) ** 2 + (xx - event.col) ** 2) / (2 * sigma_px ** 2))
            return np.clip(frame, 0, 65535).astype(np.uint16)

        source.liveFrame = live
        event.restore = lambda: setattr(source, 'liveFrame', original)
        return event

    def select(self, combo, options, wanted):
        combo.setCurrentIndex(list(options).index(wanted))

    def close(self):
        try:
            self.main.closeEvent()
        finally:
            (self._dirtools._baseUserFilesDir, self._dirtools.UserFileDirs.Root,
             self._dirtools.UserFileDirs.Config,
             self._main_cls._shouldSaveWidgetStateOnClose,
             addPoints) = self._saved
            self._image_widget._setScanGeometry = self._saved_geometry
            if addPoints is None:
                del self._napari.EmbeddedNapari.add_points
            else:
                self._napari.EmbeddedNapari.add_points = addPoints
