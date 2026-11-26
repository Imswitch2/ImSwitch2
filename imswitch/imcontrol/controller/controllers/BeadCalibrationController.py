"""Decisions behind the Camera → scan calibration window
(Hardware → Camera → scan calibration…).

The window (``view/widgets/BeadCalibrationWidget.py``) shows what this
controller hands it and reports clicks. Here: reading the two picked
recordings and what they hold (``model/bead_calibration.summarize_recording``),
the warnings across them, against the running setup and against the
camera's current grid; the fit, on a worker thread so the window stays
responsive; overwrite protection; the result and its actions. The fit, the
files written and the command line
(``python -m imswitch.imcontrol.model.bead_calibration``) are the same
functions. No hardware is touched: the current camera grid is read from the
detector manager's ROI/binning properties, nothing is set.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

from imswitch.imcommon.framework import Signal, Thread, Worker
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model import bead_calibration as bc

from ..basecontrollers import ImConWidgetController


class _JobWorker(Worker):
    """Runs one callable at a time off the GUI thread and reports back."""

    sigDone = Signal(int, object, object)      # job id, result, error

    def run(self, job_id: int, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except Exception as error:  # noqa: BLE001 - every failure is shown to the user
            self.sigDone.emit(job_id, None, error)
            return
        self.sigDone.emit(job_id, result, None)


def thumbnail_png(image: np.ndarray, points: Optional[np.ndarray], size_px: int = 200) -> bytes:
    """A small PNG of ``image`` with ``points`` ((row, col)) circled."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    fig = Figure(figsize=(2, 2), dpi=size_px / 2)
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    lo, hi = np.percentile(image, (1, 99.7))
    ax.imshow(image, cmap='gray', vmin=lo, vmax=hi if hi > lo else lo + 1)
    if points is not None and len(points):
        ax.scatter(points[:, 1], points[:, 0], s=30, facecolors='none', edgecolors='lime', linewidths=0.8)
    ax.set_axis_off()
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png')
    return buffer.getvalue()


class BeadCalibrationController(ImConWidgetController):
    """Camera → scan calibration from two bead recordings."""

    sigRunJob = Signal(int, object)

    def __init__(self, *args, recordingController=None, calibrationsFolder=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)
        self._recordingController = recordingController
        self._folder = calibrationsFolder           # None: <ImSwitchConfig>/calibrations
        self._summaries: Dict[str, Optional[bc.RecordingSummary]] = {'camera': None, 'scan': None}
        self._lastFolder: Optional[str] = None
        self._jobId = 0
        self._fitJob: Optional[int] = None
        self._pending: Dict[int, Callable[[Any, Any], None]] = {}
        self.result: Optional[bc.BeadCalibration] = None
        self.lastError: Optional[Exception] = None
        self.verification: Optional[dict] = None

        self._worker = _JobWorker()
        self._thread = Thread()
        self._worker.moveToThread(self._thread)
        self.sigRunJob.connect(self._worker.run)
        self._worker.sigDone.connect(self._jobDone)
        self._thread.start()

        w = self._widget
        w.sigBrowse.connect(self.browse)
        w.sigDetectorChosen.connect(self.chooseDetector)
        w.sigFit.connect(self.fit)
        w.sigCancel.connect(self.cancel)
        w.sigOpenFigure.connect(lambda: self._open('figure'))
        w.sigOpenFolder.connect(lambda: self._open('folder'))
        w.sigCopyReport.connect(lambda: w.copyText(w.resultText.toPlainText()))
        w.sigVerify.connect(self.verifyWithFiles)
        w.sigPredict.connect(self.predict)
        self._refreshFitEnabled()

    def finalize(self):
        self._thread.quit()
        self._thread.wait()

    # -- jobs ---------------------------------------------------------------
    def _submit(self, fn: Callable[[], Any], done: Callable[[Any, Any], None]) -> int:
        self._jobId += 1
        self._pending[self._jobId] = done
        self.sigRunJob.emit(self._jobId, fn)
        return self._jobId

    def _jobDone(self, job_id, result, error):
        done = self._pending.pop(job_id, None)
        if done is not None:
            done(result, error)

    @property
    def busy(self) -> bool:
        return self._fitJob is not None

    # -- files --------------------------------------------------------------
    def _startFolder(self) -> Optional[str]:
        if self._lastFolder:
            return self._lastFolder
        try:
            return self._recordingController.getRecFolder() if self._recordingController else None
        except Exception:  # noqa: BLE001 - a start folder is a convenience
            return None

    def browse(self, role: str, kind: str = 'file') -> None:
        title = 'Camera recording' if role == 'camera' else 'Scan (APD) recording'
        if kind == 'zarr':
            path = self._widget.askOpenFolder(f'{title} (Zarr store)', self._startFolder())
        else:
            path = self._widget.askOpenFile(title, self._startFolder())
        if path:
            self.setFile(role, path)

    def setFile(self, role: str, path, detector: Optional[str] = None) -> Optional[bc.RecordingSummary]:
        """Read ``path`` for ``role`` and show what it holds."""
        path = Path(path)
        self._lastFolder = str(path.parent)
        field = self._widget.field(role)
        try:
            summary = bc.summarize_recording(path, role, detector)
        except Exception as error:  # noqa: BLE001 - shown in the field
            self._summaries[role] = None
            field.show_error(path, f'cannot be read: {error}')
            self._updatePairWarnings()
            self._refreshFitEnabled()
            return None
        self._summaries[role] = summary
        field.show_summary(path, summary.detectors, summary.detector, summary.lines, summary.warnings)
        field.set_thumbnail(None, 'finding beads…')
        self._submitThumbnail(role, summary)
        self._updatePairWarnings()
        self._refreshFitEnabled()
        self._widget.clearResult()
        return summary

    def chooseDetector(self, role: str, detector: str) -> None:
        summary = self._summaries.get(role)
        if summary is not None:
            self.setFile(role, summary.path, detector)

    def _submitThumbnail(self, role, summary):
        image = summary.recording.image
        params = self._detectionKwargs(role)

        def work():
            found = bc.detect_beads(image, **params)
            return thumbnail_png(image, found.positions), len(found)

        def done(result, error):
            if self._summaries.get(role) is not summary:
                return      # another file was picked meanwhile
            field = self._widget.field(role)
            if error is not None:
                field.set_thumbnail(None, 'no preview')
                return
            png, count = result
            field.set_thumbnail(png)
            summary.lines.append(('Beads found', str(count)))
            field.show_summary(summary.path, summary.detectors, summary.detector, summary.lines, summary.warnings)

        self._submit(work, done)

    def _runningSetupHash(self) -> Optional[str]:
        try:
            from imswitch.imcontrol.model import replay

            return replay.computeSetupHash(self._setupInfo.to_dict())
        except Exception:  # noqa: BLE001 - the comparison is best effort
            return None

    def _currentCameraIdentity(self, detector: str):
        try:
            from imswitch.imcontrol.model.smartpipeline.live_source import frame_identity_of

            manager = self._master.detectorsManager[detector]
        except Exception:  # noqa: BLE001 - a detector this setup lacks: nothing to compare
            return None
        return frame_identity_of(manager)

    def pairWarnings(self) -> list:
        camera, scan = self._summaries['camera'], self._summaries['scan']
        identity = self._currentCameraIdentity(camera.detector) if camera else None
        return bc.compare_summaries(camera, scan, running_setup_hash=self._runningSetupHash(),
                                    current_camera_identity=identity)

    def _updatePairWarnings(self):
        self._widget.setPairWarnings(self.pairWarnings())

    def _refreshFitEnabled(self):
        ready = all(self._summaries[r] is not None for r in ('camera', 'scan'))
        self._widget.setFitEnabled(ready and not self.busy)

    # -- fit ----------------------------------------------------------------
    def _detectionKwargs(self, role):
        options = self._widget.options()
        threshold = options[f'{role}_threshold']
        sigma = options[f'{role}_sigma_px'] or None
        return {'threshold': threshold, 'sigma_px': sigma}

    def registrationOptions(self) -> bc.BeadRegistrationOptions:
        options = self._widget.options()
        return bc.BeadRegistrationOptions(
            camera=self._detectionKwargs('camera'), scan=self._detectionKwargs('scan'),
            model=options['model'], tolerance=options['tolerance_um'] or None,
            max_residual=options['max_residual_um'], allow_mirror=options['allow_mirror'])

    def fit(self) -> bool:
        """Start the fit on the worker thread; False when it cannot start."""
        if self.busy:
            return False
        camera, scan = self._summaries['camera'], self._summaries['scan']
        if camera is None or scan is None:
            self._widget.showMessage('Camera → scan calibration', 'Pick both recordings first.')
            return False
        calibration_id = self._widget.options()['calibration_id'] or 'cam_to_scan'
        overwrite = False
        if bc.record_path(calibration_id, self._folder).exists():
            stamp_id = f'{calibration_id}_<date-time>'
            answer = self._widget.askOverwrite(calibration_id, stamp_id)
            if answer == 'cancel':
                return False
            overwrite = True
            keep_old = answer == 'keep'
        else:
            keep_old = False
        options = self.registrationOptions()
        folder = self._folder
        cam_path, cam_det = camera.path, camera.detector
        scan_path, scan_det = scan.path, scan.detector

        def work():
            # Fit without saving first: a refused fit writes nothing and
            # leaves an existing calibration untouched.
            result = bc.calibrate_from_files(cam_path, scan_path, calibration_id, camera_detector=cam_det,
                                             scan_detector=scan_det, options=options, folder=folder, save=False)
            archived = bc.archive_calibration(calibration_id, folder) if keep_old else None
            result.paths = bc.save_bead_calibration(result, folder=folder, overwrite=overwrite or keep_old)
            return result, archived

        self.result = None
        self.lastError = None
        self._widget.clearResult()
        self._widget.setBusy(True, 'Fitting… (finding and pairing beads)')
        self._fitJob = self._submit(work, self._fitDone)
        return True

    def cancel(self) -> None:
        """Stop waiting for the running fit; its result, when it arrives, is
        dropped. (The computation cannot be interrupted midway; it finishes
        on the worker thread and nothing of it is shown.)"""
        if self._fitJob is None:
            return
        job = self._fitJob
        self._fitJob = None

        def dropped(result, error):
            # The fit saved before the cancel landed: say so instead of hiding it.
            if error is None and result is not None:
                calibration, _ = result
                self._widget.setResult(False, 'Cancelled — but the fit had already finished and was saved.',
                                       calibration.report())
        self._pending[job] = dropped
        self._widget.setBusy(False, 'Cancelled.')
        self._refreshFitEnabled()

    def _fitDone(self, result, error):
        self._fitJob = None
        self._widget.setBusy(False, '')
        self._refreshFitEnabled()
        if error is not None:
            self.lastError = error
            verdict, advice = bc.plain_reason(error)
            self._widget.setResult(False, f'Refused: {verdict} {advice} Nothing was saved.', str(error))
            return
        calibration, archived = result
        self.result = calibration
        text = self.resultText(calibration, archived)
        g = calibration.registration.geometry()
        self._widget.setResult(
            True, f'Accepted: {len(calibration.registration.camera_points)} beads paired, residual RMS '
                  f'{calibration.registration.rms * 1000:.0f} nm, rotation {g["rotation_deg"]:.2f}°, '
                  f'{"mirrored" if g["mirror"] else "not mirrored"}.', text)
        self._widget.setPrediction('')

    @staticmethod
    def resultText(calibration: bc.BeadCalibration, archived: Optional[str] = None) -> str:
        reg = calibration.registration
        g = reg.geometry()
        a0, a1 = reg.target_axes
        configured = calibration.camera_context_pixel()
        inliers = int(reg.inliers.sum())
        lines = [
            f"Beads found: camera {len(reg.camera_detections)}, scan {len(reg.scan_detections)}; "
            f"paired {len(reg.camera_points)}, inliers {inliers}",
            f"Residual: RMS {reg.rms * 1000:.1f} nm ({reg.rms / reg.scale:.3f} camera px), "
            f"max {reg.max * 1000:.1f} nm ({reg.max / reg.scale:.3f} camera px)",
            f"Rotation {g['rotation_deg']:.3f}°, {'MIRRORED' if g['mirror'] else 'not mirrored'}",
            f"Scale {g['scale']:.5f} µm per camera pixel"
            + (f" (configured pixel size {configured:.5f} µm)" if configured else ""),
            f"Camera pixel (0, 0) lands at {a0} = {g['translation'][0]:.3f} µm, {a1} = {g['translation'][1]:.3f} µm",
            f"Model: {calibration.transform.provenance.get('estimator')} — {reg.model_rule}",
            f"Pipeline scan step: axes [\"{a0}\", \"{a1}\"]; transform id '{calibration.calibration_id}'",
        ]
        lines += [f"Warning: {w}" for w in reg.warnings + list(calibration.notes)]
        if archived:
            lines.append(f"The previous calibration was kept as '{archived}'.")
        for kind, path in calibration.paths.items():
            lines.append(f"{kind.capitalize()}: {path}")
        lines += ["", "Full report:", calibration.report()]
        return "\n".join(lines)

    # -- result actions ------------------------------------------------------
    def _open(self, what):
        if self.result is None:
            return
        path = self.result.paths.get('figure') if what == 'figure' else Path(self.result.paths['record']).parent
        if path:
            self._widget.openPath(path)

    def predict(self, row: float, col: float) -> Optional[dict]:
        if self.result is None:
            return None
        centre = bc.predict_scan_centre(self.result.transform, row, col)
        self._widget.setPrediction(', '.join(f'{axis} = {value:.3f} µm' for axis, value in centre.items()))
        return centre

    def verifyWithFiles(self, files=None) -> None:
        """Verify the current calibration on another pair of files (asked
        for when not given), off the GUI thread."""
        if self.result is None:
            return
        files = files or self._widget.askVerificationFiles(self._startFolder())
        if not files:
            return
        camera_file, scan_file = files
        transform = self.result.transform
        cam_det = self.result.camera.detector
        scan_det = self.result.scan.detector

        def work():
            return bc.verify_from_files(transform, camera_file, scan_file, camera_detector=_ifIn(camera_file, cam_det),
                                        scan_detector=_ifIn(scan_file, scan_det))

        def done(report, error):
            self._widget.setBusy(False, '')
            if error is not None:
                self._widget.showMessage('Verification', f'Could not verify: {error}')
                return
            self.verification = report
            problems = report.get('grid_problems') or []
            text = (f"{report['matched']} of {report['scan_beads']} scan beads have a camera bead within 0.5 µm "
                    f"after the transform.\nDistance RMS {report['rms_um'] * 1000:.0f} nm, median "
                    f"{report['median_um'] * 1000:.0f} nm, max {report['max_um'] * 1000:.0f} nm; mean offset "
                    f"{', '.join(f'{v * 1000:.0f}' for v in report['mean_offset_um'])} nm.\n"
                    + ('Camera grid: ' + '; '.join(problems) if problems else 'Camera grid: as calibrated.'))
            self._widget.showMessage('Verification', text)

        self._widget.setBusy(True, 'Verifying…')
        self._submit(work, done)


def _ifIn(path, detector):
    try:
        return detector if detector in bc.list_detectors(path) else None
    except Exception:  # noqa: BLE001
        return None
