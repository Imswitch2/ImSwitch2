import json
import os
import time

from qtpy import QtCore

from imswitch.imcommon.model import dirtools, initLogger
from imswitch.imcommon.model.api import _UIThreadExecWrapper
from imswitch.imcommon.model.smartpipeline import Live, SmartPipelineDocument
from imswitch.imcommon.model.workflow.steps import Process, WorkflowError
from imswitch.imcontrol.model.smartpipeline import ArmError, EventLog, RunMode
from imswitch.imcontrol.model.smartpipeline.live_actions import live_backend
from imswitch.imcontrol.model.smartpipeline.live_source import LiveFrameSource
from imswitch.imcontrol.model.smartpipeline.mock import FunctionStepRunner
from imswitch.imcontrol.model.smartpipeline.run import (
    ARMED, RUNNING, STOPPING, PipelineRun,
)
from imswitch.imcontrol.model.smartpipeline.calibrations import CalibrationError, load_calibrations
from imswitch.imcontrol.model.smartpipeline.designer import pipeline_catalog
from ..basecontrollers import ImConWidgetController

_pipelinesFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'smart_pipelines')
_calibrationsFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'calibrations')
_stateFilePath = os.path.join(dirtools.UserFileDirs.Config, 'smart_pipeline.json')
_editorPreferencesPath = os.path.join(dirtools.UserFileDirs.Config, 'smart_pipeline_editor.json')

_STATUS_PERIOD_MS = 500
_CLOSE_JOIN_S = 5.0
#: Camera frames saved with a triggered event: before the trigger, after the scan.
_EVENT_PRE_FRAMES = 5
_EVENT_POST_FRAMES = 5


class SmartPipelineController(ImConWidgetController):
    """ Linked to SmartPipelineWidget. Loads a smart-pipeline file, arms it
    against the live detector, the positioners and the setup modes, runs
    the engine on a worker thread and shows its event log.

    Processing steps run through ImProcess's processors, found lazily when
    the ImProcess module is loaded; without it a pipeline with processing
    steps does not arm. Transforms come from ``calibrations/*.json`` in the
    user's ImSwitch folder (one transform dict per file). """

    SMART_MODE_WORKFLOW = 'SmartPipeline'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

        self._path = None
        self._document = None
        self._issues = []
        self._run = None
        self._source = None
        self._smartModeService = None
        self._registry = None
        self._editor = None
        self._closing = False
        self._recorder = None         # the run's TriggeredScanRecorder (saves the triggered scans)
        self._saveWarnings = []       # why an Experiment run could not save, shown in a dry run
        self._shownGeometry = {}      # detector -> the scan geometry the live view shows (axes), or None
        self._marksGeometry = None    # the geometry the overlay's marks belong to
        shown = getattr(self._commChannel, 'sigScanGeometryShown', None)
        if shown is not None:
            shown.connect(self._onScanGeometryShown)
        self._marshal = _UIThreadExecWrapper(lambda fn: fn())

        self._statusTimer = QtCore.QTimer()
        self._statusTimer.setInterval(_STATUS_PERIOD_MS)
        self._statusTimer.timeout.connect(self._refreshStatus)

        self._widget.sigBrowseClicked.connect(self.browse)
        self._widget.sigReloadClicked.connect(self.reload)
        self._widget.sigDesignClicked.connect(self.openDesigner)
        self._widget.sigArmClicked.connect(self.arm)
        self._widget.sigStopClicked.connect(self.stop)
        self._widget.sigSaveLogClicked.connect(self.saveLog)
        self._widget.sigModeChanged.connect(lambda _mode: (self._saveState(), self._updateButtons()))

        state = _loadState()
        if state.get('mode') in ('visualize', 'validate', 'experiment'):
            self._widget.setMode(state['mode'])
        if state.get('path'):
            self.setPipelinePath(state['path'])
        else:
            self._updateButtons()

    def setSmartModeService(self, service):
        """ Injected by the main controller: how setup-mode roles are applied. """
        self._smartModeService = service

    # ------------------------------------------------------------------ #
    # The pipeline file                                                   #
    # ------------------------------------------------------------------ #
    def browse(self):
        folder = os.path.dirname(self._path) if self._path else _pipelinesFolderPath
        os.makedirs(folder, exist_ok=True)
        path = self._widget.askForPipelinePath(folder)
        if path:
            self.setPipelinePath(path)

    def setPipelinePath(self, path):
        self._path = os.path.normpath(path) if path else None
        self._widget.setPipelinePath(self._path)
        self.reload()
        self._saveState()

    def reload(self):
        """ Reads the file again and checks it. Returns whether it loaded. """
        self._document = None
        self._issues = []
        if not self._path:
            self._widget.setSummary('')
            self._widget.setIssues([])
            self._updateButtons()
            return False
        try:
            document = SmartPipelineDocument.load(self._path)
        except (OSError, ValueError, WorkflowError) as e:
            self._issues = [f'Cannot load the pipeline: {e}']
            self._widget.setSummary('')
        else:
            self._document = document
            self._issues = self._check(document)
            self._widget.setSummary(self._summaryOf(document))
        self._widget.setIssues(self._issues)
        self._updateButtons()
        return self._document is not None

    def _check(self, document):
        try:
            transforms = self._transforms()
        except ValueError as e:
            return [str(e)]
        registry = self._improcessRegistry() if self._needsImprocess(document) else None
        issues = [str(issue) for issue in document.validate(
            registry=registry, actions=self._catalog(), transforms=transforms,
        )]
        if self._needsImprocess(document) and registry is None:
            issues.append('the pipeline has processing steps, which need the ImProcess module')
        source = document.source
        if isinstance(source, Live) and source.detector:
            names = list(self._master.detectorsManager.getAllDeviceNames())
            if source.detector not in names:
                issues.append(f'no detector {source.detector!r} in this setup (have: {", ".join(names) or "none"})')
        elif source is not None and not isinstance(source, Live):
            issues.append('a live run needs a live detector source; a recording source replays offline only')
        return issues

    @staticmethod
    def _summaryOf(document):
        steps = document.steps
        actions = [step.id for step in document.actions]
        source = document.source
        where = f'{source.detector}' if isinstance(source, Live) else 'a recording'
        roles = document.settings.roles
        parts = [f'{document.name}: {len(steps)} steps from {where}']
        if actions:
            parts.append(f'actions: {", ".join(actions)}')
        parts.append('roles: ' + (', '.join(f'{k}={v}' for k, v in roles.items()) if roles else 'none (visualize only)'))
        return ' · '.join(parts)

    @staticmethod
    def _needsImprocess(document):
        return any(isinstance(step, Process) for step in document.steps)

    def _improcessRegistry(self):
        """ ImProcess's processors for the pipeline's steps: a registry of
        every built-in and drop-in plugin, built once. The module's own
        registry holds only what the setup's processing block enables for
        its panels, which is not what a pipeline file may name. """
        if not self._moduleCommChannel.isModuleRegistered('improcess'):
            return None
        if self._registry is not None:
            return self._registry
        try:
            from imswitch.improcess.workflows.runtime import bootstrap_registry

            self._registry = bootstrap_registry(user_plugins=True)
        except Exception:
            self.__logger.warning('Could not build the ImProcess plugin registry', exc_info=True)
            try:
                from imswitch.improcess.reconstructors.registry import get_registry

                self._registry = get_registry()
            except Exception:
                return None
        return self._registry

    def _stepRunner(self, document):
        registry = self._improcessRegistry()
        if registry is None or not self._needsImprocess(document):
            return FunctionStepRunner({})
        from imswitch.improcess.workflows.smartpipeline import ProcessorStepRunner

        return ProcessorStepRunner(registry, logger=self.__logger)

    @staticmethod
    def _transforms():
        try:
            return load_calibrations(_calibrationsFolderPath)
        except CalibrationError as e:
            raise ValueError(str(e)) from e

    def _catalog(self):
        from imswitch.imcontrol.model.smartpipeline.catalog import catalog

        return catalog()

    # ------------------------------------------------------------------ #
    # The designer                                                        #
    # ------------------------------------------------------------------ #
    def designerCatalog(self):
        """ The editor's catalogue for this setup: ImProcess's processors (when
        that module is loaded), the pipeline step kinds with the setup's
        detectors, the calibrations and the actions, and the recipes. """
        detectors = list(self._master.detectorsManager.getAllDeviceNames())
        try:
            transforms = self._transforms().ids()
        except ValueError:
            transforms = ()
        positioners = list(self._master.positionersManager.getAllDeviceNames())
        axes = []
        for name in positioners:
            for axis in getattr(self._master.positionersManager[name], 'axes', ()) or ():
                axes.append(f'{name}:{axis}')
        defaults = {'axes': axes[:2] if axes else ['Y', 'X']}
        return pipeline_catalog(registry=self._improcessRegistry(), detectors=detectors, transforms=transforms,
                                actions=self._catalog(), recipe_defaults=defaults)

    def openDesigner(self):
        """ Opens the workflow editor on the chosen pipeline (or a new one). """
        from imswitch.imcommon.model.workfloweditor.preferences import EditorPreferences
        from imswitch.imcommon.view.workfloweditor.editor import MainWindow

        editor = self._editor
        if editor is not None:
            editor.show()
            editor.raise_()
            editor.activateWindow()
            return editor
        os.makedirs(_pipelinesFolderPath, exist_ok=True)
        editor = MainWindow(self.designerCatalog(), start_folder=_pipelinesFolderPath,
                            preferences=EditorPreferences(_editorPreferencesPath),
                            title='Smart pipeline designer')
        editor.host = _DesignerHost(self)
        editor.sig_closed.connect(self._onDesignerClosed)
        self._editor = editor
        if self._path and os.path.isfile(self._path):
            editor.open_file(self._path)
        editor.show()
        return editor

    def _onDesignerClosed(self):
        editor, self._editor = self._editor, None
        if editor is None:
            return
        saved = getattr(editor.document, 'path', None)
        if saved:
            self.setPipelinePath(str(saved))      # the file as the designer left it

    # ------------------------------------------------------------------ #
    # Arm / stop                                                          #
    # ------------------------------------------------------------------ #
    def arm(self):
        if self._run is not None and self._run.state in (ARMED, RUNNING, STOPPING):
            return
        if self._document is None or self._issues:
            self._updateButtons()
            return
        document = self._document
        mode = RunMode.of(self._widget.getMode())
        source = LiveFrameSource(self._master.detectorsManager, document.source.detector,
                                 logger=self.__logger)
        scan = None
        workflow = getattr(self._commChannel, 'scanWorkflow', None)
        recorder = None
        if workflow is not None:
            recorder = self._makeRecorder(document)
            scan = {'controllers': self._commChannel.controllerRegistry, 'workflow': workflow,
                    'capture': source.capture_latest, 'recorder': recorder}
        saveProblems = self._saveProblems(document, recorder)
        self._saveWarnings = []
        if saveProblems:
            if mode == RunMode.EXPERIMENT:
                # A triggered scan that cannot be saved is not taken at all.
                self._widget.setIssues([f'not armed: the triggered scans could not be saved -- {problem}'
                                        for problem in saveProblems])
                self._widget.setStatus('not armed')
                return
            self._saveWarnings = saveProblems
        actions = live_backend(self._master.positionersManager, snap=self._snap, marshal=self._marshal,
                               logger=self.__logger, scan=scan)
        try:
            transforms = self._transforms()
            steps = self._stepRunner(document)
        except Exception as e:
            self._widget.setIssues([str(e)])
            return
        log = EventLog(keep=5000, sink=self._onLogEntry)
        run = PipelineRun(
            document, source=source, steps=steps, actions=actions, transforms=transforms, mode=mode,
            apply_mode=self._applyMode, owner=f'smart-pipeline:{document.name}', log=log,
            logger=self.__logger, registry=self._improcessRegistry(), on_finished=self._onRunFinished,
            on_overlay=self._onOverlay,
        )
        self._widget.clearLog()
        try:
            run.arm()
        except ArmError as e:
            self._widget.setIssues(str(e).splitlines())
            self._widget.setStatus('not armed')
            return
        except Exception as e:
            self.__logger.exception('Smart pipeline: arming failed')
            self._widget.setIssues([f'{type(e).__name__}: {e}'])
            self._widget.setStatus('not armed')
            return
        self._widget.setIssues([f'note: in Experiment the triggered scans could not be saved: {p}'
                                for p in self._saveWarnings])
        self._run = run
        self._source = source
        self._recorder = recorder
        if recorder is not None and self._savesScans(document) and mode == RunMode.EXPERIMENT:
            log.append(0, time.monotonic(), 'pipeline', 'saving', folder=recorder.folder,
                       detectors=list(recorder.detectors), format=recorder.save_format)
            recorder.archive = self._startArchive(run, document, source, recorder, transforms, log)
        self._commChannel.sigUpdateImage.connect(source.on_image)
        self._showOverlay(True)
        run.start()
        self._statusTimer.start()
        self._updateButtons()
        self._refreshStatus()

    # ------------------------------------------------------------------ #
    # Saving the triggered scans                                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scanSteps(document):
        from imswitch.imcommon.model.smartpipeline import action_chain

        return [step for step in action_chain(document.workflow) if step.action == 'run-scan']

    def _savesScans(self, document):
        from imswitch.imcontrol.model.smartpipeline.catalog import RUN_SCAN

        return any(bool({**RUN_SCAN.params, **step.params}.get('save', True))
                   for step in self._scanSteps(document))

    def _recordingsFolder(self):
        recording = self._commChannel.controllerRegistry().get('Recording')
        if recording is None:
            return None
        try:
            return recording.getRecFolder()
        except Exception:
            return None

    def _stagePosition(self):
        """ Where the stage positioners are now: {positioner: {axis: um}},
        the positioners that position (not those that only scan). """
        found = {}
        for name, positioner in self._master.positionersManager:
            if getattr(positioner, 'forScanning', False) or not getattr(positioner, 'forPositioning', True):
                continue
            try:
                found[name] = {str(k): float(v) for k, v in dict(positioner.position).items()}
            except Exception:
                continue
        return found

    def _makeRecorder(self, document):
        """ The recorder of this run's triggered scans: a folder of its own
        under the Recording panel's folder, the scan-driven detectors. """
        from imswitch.imcontrol.model.smartpipeline.event_recording import (
            TriggeredScanRecorder, run_folder_name, scan_driven_detectors,
        )

        if not self._scanSteps(document):
            return None
        base = self._recordingsFolder()
        folder = os.path.join(base, 'smart_pipeline', run_folder_name(document.name)) if base else ''
        tiling = getattr(self._setupInfo, 'tiling', None)
        baseAttrs = {'SmartPipeline:pipeline': document.name,
                     'SmartPipeline:pipeline_file': self._path or '',
                     'SmartPipeline:run_folder': folder}
        if tiling is not None and getattr(tiling, 'xyPositioner', None):
            baseAttrs['SmartPipeline:xy_positioner'] = tiling.xyPositioner
        return TriggeredScanRecorder(
            self._commChannel.controllerRegistry, self._marshal, folder=folder,
            detectors=scan_driven_detectors(self._master.detectorsManager), save_format='HDF5',
            base_attrs=baseAttrs, logger=self.__logger, stage_position=self._stagePosition,
        )

    def _startArchive(self, run, document, source, recorder, transforms, log):
        """ The records of an Experiment run besides its scans: the armed
        document, the run header, per event the camera frames, ROIs and
        traces, the summary, the region file -- into the scans' folder. """
        from imswitch.imcommon.model.smartpipeline import Transform
        from imswitch.imcontrol.model.smartpipeline.boxes import transform_record
        from imswitch.imcontrol.model.smartpipeline.event_archive import EventArchive, roi_snapshot

        roiSteps = [step.id for step in document.workflow.steps
                    if isinstance(step, Process) and step.processor == 'roi-dff']
        engine = run.engine

        def snapshot():
            if not roiSteps:
                return None
            with engine._lock:
                return roi_snapshot(engine._state.get(roiSteps[0]))

        def report(message):
            log.append(engine._tick, time.monotonic(), 'records', 'save-failed', error=message)

        source.keep_frames = _EVENT_PRE_FRAMES + _EVENT_POST_FRAMES + 8
        archive = EventArchive(recorder.folder, source=source, snapshot=snapshot, pre_frames=_EVENT_PRE_FRAMES,
                               post_frames=_EVENT_POST_FRAMES, region=recorder.region,
                               stage_position=self._stagePosition, report=report, logger=self.__logger)
        records = {}
        for step in document.workflow.steps:
            if isinstance(step, Transform):
                transform = transforms.get(step.transform)
                if transform is not None:
                    try:
                        records[step.id] = transform_record(transform)
                    except Exception:
                        records[step.id] = {'id': step.transform}
        identity = getattr(source, 'identity', None)
        header = {'pipeline': document.name, 'pipeline_file': self._path, 'mode': RunMode.EXPERIMENT.value,
                  'detector': source.detector, 'camera_grid': getattr(identity, '__dict__', None),
                  'scan_detectors': list(recorder.detectors), 'transforms': records,
                  'roi_steps': roiSteps, 'pre_frames': _EVENT_PRE_FRAMES, 'post_frames': _EVENT_POST_FRAMES,
                  'setup': getattr(getattr(self, '_options', None), 'setupFileName', None)}
        archive.start(header, document.to_json())
        return archive

    def _saveProblems(self, document, recorder):
        """ Why the triggered scans of ``document`` could not be saved, or
        []. Checked at every arm: a refusal in Experiment, a note in a dry run. """
        if not self._savesScans(document):
            return []
        if recorder is None:
            return ['this setup has no scan workflow to run and record the scans with']
        problems = []
        for step in self._scanSteps(document):
            problems.extend(recorder.problems(str(step.params.get('source') or 'Scan')))
        return list(dict.fromkeys(problems))

    def stop(self):
        run = self._run
        if run is None:
            return
        run.stop()
        self._updateButtons()

    def _onRunFinished(self, run):
        # worker thread: the run's records are completed here (the region
        # file, the summary), then the GUI thread takes over
        recorder = self._recorder if run is self._run else None
        archive = getattr(recorder, 'archive', None)
        if archive is not None:
            try:
                if not archive.close():
                    self.__logger.warning('Smart pipeline: the run records were still being written at the end')
            except Exception:
                self.__logger.warning('Smart pipeline: closing the run records failed', exc_info=True)
        self._invokeOnControllerThread(lambda: self._finishRun(run))

    def _finishRun(self, run):
        if run is not self._run:
            return
        source = self._source
        if source is not None:
            try:
                self._commChannel.sigUpdateImage.disconnect(source.on_image)
            except (TypeError, RuntimeError):
                pass
        self._statusTimer.stop()
        self._showOverlay(False)
        self._refreshStatus()
        self._updateButtons()

    def _showOverlay(self, shown):
        overlay = self._widget.getOverlay()
        try:
            if shown:
                self._widget.setOverlayPoints([], [])
                self._commChannel.sigAddItemToVb.emit(overlay)
            else:
                self._widget.setOverlayPoints([], [])
                self._commChannel.sigRemoveItemFromVb.emit(overlay)
        except Exception:
            self.__logger.debug('Smart pipeline: the live view took no overlay', exc_info=True)

    @staticmethod
    def _axesOf(geometry):
        axes = (geometry or {}).get('axes') if isinstance(geometry, dict) else None
        return [tuple(sorted(dict(axis).items())) for axis in axes] if axes else None

    def _onScanGeometryShown(self, detectorName, shown):
        """ The live view shows another scan's pixels: marks measured on an
        earlier scan no longer belong to what is on screen. """
        geometry = getattr(shown, 'geometry', None)
        axes = self._axesOf(geometry.to_dict()) if geometry is not None and hasattr(geometry, 'to_dict') else None
        self._shownGeometry[str(detectorName)] = axes
        if self._marksGeometry is not None and self._marksGeometry != axes:
            self._marksGeometry = None
            self._widget.setOverlayPoints([], [])

    def _onOverlay(self, stepId, value, frame=None):
        """ The engine's overlay sink (worker thread): points in the detector's
        pixels onto the live view -- only when the frame they were measured
        on is the one the view shows (a scan's geometry; a camera frame has
        none and always is). Points in another frame are not drawn. """
        from imswitch.imcommon.model.smartpipeline import frame_of, points_of

        coordinates = points_of(value)
        if coordinates is None:
            return
        tag = frame_of(value)
        if not tag.startswith('detector:'):
            return
        geometry = self._axesOf((getattr(frame, 'metadata', None) or {}).get('geometry')) if frame is not None else None
        detector = tag.split(':', 1)[1]
        if coordinates.shape[0] and coordinates.shape[1] >= 2:
            ys, xs = coordinates[:, 0].tolist(), coordinates[:, 1].tolist()
        else:
            ys, xs = [], []

        def draw():
            if geometry is not None and self._shownGeometry.get(detector) not in (None, geometry):
                return      # the view moved on to another scan
            self._marksGeometry = geometry
            self._widget.setOverlayPoints(xs, ys)

        self._invokeOnControllerThreadIfNeeded(draw)

    def _onLogEntry(self, entry):
        text = str(entry)
        self._invokeOnControllerThreadIfNeeded(lambda: self._widget.appendLog(text))
        if entry.event in ('started', 'would', 'skipped') and entry.detail.get('action'):
            # The marks belong to the frame they were measured on; a hardware
            # action (a scan) changes what the live view shows, so they come
            # off until the next tick draws its own. A wait keeps them.
            spec = self._catalog().get(str(entry.detail.get('action')))
            if spec is None or spec.hardware:
                self._invokeOnControllerThreadIfNeeded(lambda: self._widget.setOverlayPoints([], []))

    def _applyMode(self, modeName):
        """ Applies a setup mode on the GUI thread; returns a problem text or None. """
        service = self._smartModeService
        controller = getattr(service, 'setupModeController', None) if service is not None else None
        if controller is None:
            return 'setup modes are not available in this session'

        def apply():
            available = list(controller.listSetupModes() or [])
            if modeName not in available:
                return f'no setup mode {modeName!r} (have: {", ".join(available) or "none"})'
            outcome = controller.applySetupMode(modeName)
            failed = list(getattr(outcome, 'failedComponents', None) or getattr(outcome, 'failed', None) or [])
            return f'components failed: {", ".join(map(str, failed))}' if failed else None

        try:
            return self._marshal(apply)
        except Exception as e:
            return f'{type(e).__name__}: {e}'

    def _snap(self, detectorName):
        recording = self._commChannel.controllerRegistry().get('Recording')
        if recording is None:
            raise RuntimeError('no Recording panel to snap with')
        return recording.snap()

    # ------------------------------------------------------------------ #
    # Status and log                                                      #
    # ------------------------------------------------------------------ #
    def _refreshStatus(self):
        run = self._run
        if run is None:
            self._widget.setStatus('')
            return
        status = run.status()
        parts = [status['state'], status['mode']]
        parts.append(f'{status["ticks"]} ticks')
        dropped = status['dropped_busy'] + status['dropped_stale']
        inbox = status.get('inbox') or {}
        dropped += inbox.get('replaced', 0) + inbox.get('aged_out', 0)
        if dropped:
            parts.append(f'{dropped} dropped')
        if status['actions_started']:
            parts.append(f'{status["actions_completed"]}/{status["actions_started"]} actions')
        if status['busy']:
            parts.append('busy')
        if status['error']:
            parts.append(f'error: {status["error"]}')
        if status['unfinished_action']:
            parts.append(f'{status["unfinished_action"]} still running')
        self._widget.setStatus(' · '.join(parts), json.dumps(status, indent=1, default=str))

    def saveLog(self):
        run = self._run
        if run is None:
            return
        folder = os.path.dirname(self._path) if self._path else _pipelinesFolderPath
        path = self._widget.askForLogPath(folder)
        if not path:
            return
        if path.lower().endswith('.json'):
            run.log.write_json(path)
        else:
            run.log.write_csv(path)

    def _updateButtons(self):
        run = self._run
        active = run is not None and run.state in (ARMED, RUNNING, STOPPING)
        ready = self._document is not None and not self._issues and not self._closing
        if active:
            self._widget.setArmEnabled(False, 'The pipeline is running')
        elif not self._document:
            self._widget.setArmEnabled(False, 'Choose a pipeline file')
        elif self._issues:
            self._widget.setArmEnabled(False, 'Fix the issues listed above')
        else:
            self._widget.setArmEnabled(ready)
        self._widget.setStopEnabled(active and run.state != STOPPING)
        self._widget.setModeEnabled(not active)

    # ------------------------------------------------------------------ #
    # State and shutdown                                                  #
    # ------------------------------------------------------------------ #
    def _saveState(self):
        try:
            os.makedirs(os.path.dirname(_stateFilePath), exist_ok=True)
            with open(_stateFilePath, 'w', encoding='utf-8') as file:
                json.dump({'path': self._path, 'mode': self._widget.getMode()}, file, indent=2)
        except OSError:
            self.__logger.warning('Could not save the smart pipeline state', exc_info=True)

    def closeEvent(self):
        self._closing = True
        self._statusTimer.stop()
        editor, self._editor = self._editor, None
        if editor is not None:
            try:
                editor.sig_closed.disconnect(self._onDesignerClosed)
            except (TypeError, RuntimeError):
                pass
            editor.close()
        run = self._run
        if run is not None:
            run.stop()
            run.join(_CLOSE_JOIN_S)
        self._saveState()
        super().closeEvent()

    def shutdownComplete(self):
        run = self._run
        if run is not None and run.alive:
            return False
        source = self._source
        if source is not None and source.release_pending:
            return source.retry_release()
        return True


class _DesignerHost:
    """ What the editor asks its host for; the designer runs nothing itself. """

    def __init__(self, controller):
        self._controller = controller

    def runRefusal(self):
        return 'The designer does not run pipelines: save, then Arm from the Smart Pipeline panel.'


def _loadState():
    try:
        with open(_stateFilePath, 'r', encoding='utf-8') as file:
            state = json.load(file)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
