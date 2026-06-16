from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BEAD_REC_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'BeadRecController.py'
)


def test_bead_rec_controller_uses_pure_reconstruction_helpers():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    assert 'from imswitch.imcontrol.model.bead_recognition import (' in source
    assert 'analyze_donut,' in source
    assert 'BeadAcquisitionConfig,' in source
    assert 'BeadRecResultRecord,' in source
    assert 'BeadWorkerUpdate,' in source
    assert 'create_reconstruction_buffer(self._config.scan_dims)' in source
    assert 'find_bead_center,' in source
    assert 'reconstruction_image(self.recIm, self.dims)' in source
    assert 'rescale_reconstruction_to_pixel_size(im, self.stepSizes)' in source
    assert 'normalize_roi_bounds(self._getRoiBounds(), keptFrames[0].shape)' in source
    assert 'append_roi_means(\n                                recIm,' in source


def test_bead_rec_controller_no_longer_uses_raw_resize_or_direct_roi_mean():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    assert 'np.resize(self.recIm' not in source
    assert 'img = img[y0:y1, x0:x1]' not in source
    assert 'np.mean(img)' not in source
    assert 'from skimage import measure, morphology' not in source
    assert 'from scipy.signal import find_peaks' not in source


def test_bead_rec_center_query_and_donut_plotting_use_model_analysis():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    # Center query tries model fits first and falls back to the legacy search
    assert 'result = fit_bead(self.imDisplay, model_key, params=self._widget.analysisPrm)' in source
    assert 'result = find_bead_center(self.imDisplay, mode, self._widget.analysisPrm)' in source
    # Donut analysis goes through the pure model function, no inline analysis
    assert 'result = analyze_donut(self.imDisplay, self._widget.analysisPrm)' in source
    assert 'measure.label' not in source
    assert 'find_peaks' not in source
    # Plotting must not block the GUI event loop from the controller
    assert 'plt.show()' not in source
    assert 'import matplotlib.pyplot' not in source


def test_bead_worker_uses_narrow_callables_instead_of_controller_access():
    source = BEAD_REC_CONTROLLER_PATH.read_text()
    worker_source = source[source.index('class BeadWorker'):]

    # The gate is a narrow controller callable that arms only after the
    # pre-scan camera backlog is flushed (not raw isScanRunning).
    assert 'BeadWorker(\n            isScanRunning=self._scanFramesReady,' in source
    assert 'getFrames=self._getCurrentDetectorChunk' in source
    assert 'getRoiBounds=self._getBeadRoiBounds' in source
    assert 'sigNewChunk = Signal(object)' in worker_source
    assert 'sigProgress = Signal(int, int)' in worker_source
    assert 'isScanRunning: Callable[[], bool],' in worker_source
    assert 'getFrames: Callable[[], Sequence[np.ndarray]],' in worker_source
    assert 'getRoiBounds: Callable[[], Sequence[int]],' in worker_source
    assert 'self._config = None' in worker_source
    assert 'def start(self, config: BeadAcquisitionConfig) -> None:' in worker_source
    assert 'def configure(self, config: BeadAcquisitionConfig) -> None:' in worker_source
    assert 'self.__controller' not in worker_source
    assert '._widget' not in worker_source
    assert '._master' not in worker_source
    assert '._commChannel' not in worker_source
    assert 'self.sigNewChunk.emit(workerUpdate)' in worker_source
    assert 'self.sigProgress.emit(workerUpdate.filled_pixels, workerUpdate.total_pixels)' in worker_source


def test_bead_rec_controller_uses_acquisition_config_and_worker_updates():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    assert 'def _createAcquisitionConfig(self) -> BeadAcquisitionConfig:' in source
    assert 'return BeadAcquisitionConfig.from_scan_dims(self.dims, frames_per_pixel=self.framesPerPixel)' in source
    assert 'config = self._createAcquisitionConfig()' in source
    assert 'self.beadWorker.start(config)' in source
    assert 'self._widget.updateProgress(0, config.total_pixels)' in source
    assert 'if isinstance(recIm, BeadWorkerUpdate):' in source
    assert 'self._updateProgress(recIm.filled_pixels, recIm.total_pixels)' in source


def test_bead_rec_controller_updates_widget_status_and_progress():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    assert 'self.beadWorker.sigProgress.connect(self._updateProgress)' in source
    assert 'def _updateProgress(self, current: int, total: int) -> None:' in source
    assert 'self.beadWorker.sigWarning.connect(self._widget.setStatusText)' in source
    assert 'self._widget.setStatusText("Bead reconstruction running")' in source
    assert 'self._widget.setStatusText("Bead reconstruction stopped")' in source
    assert 'self._widget.setStatusText("Bead reconstruction scan complete")' in source


def test_bead_rec_controller_uses_result_records_and_passive_state():
    source = BEAD_REC_CONTROLLER_PATH.read_text()

    assert 'self.resultRecords = []' in source
    assert 'def _insertResultRecord(self, index: int, record: BeadRecResultRecord) -> None:' in source
    assert 'BeadRecResultRecord(' in source
    assert 'self.resultRecords.insert(index, record)' in source
    assert 'record.image' in source
    assert "getWidgetStatePersistence().register('BeadRecController', self)" in source
    assert 'def getWidgetState(self) -> dict[str, object]:' in source
    assert '"analysis_parameters": BeadAnalysisParameters.from_mapping(' in source
    assert '"result_metadata": [' in source
    assert 'def setWidgetState(self, state: dict[str, object]) -> None:' in source
    assert 'def getStateSchemaVersion(self) -> int:' in source
