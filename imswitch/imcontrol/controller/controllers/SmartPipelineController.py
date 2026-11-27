import json
import os
import threading
import time

import numpy as np

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
    ARMED, FAILED, RUNNING, STOPPED, STOPPING, PipelineRun,
)
from imswitch.imcontrol.model.smartpipeline.calibrations import CalibrationError, load_calibrations
from imswitch.imcontrol.model.smartpipeline.designer import pipeline_catalog
from ..basecontrollers import ImConWidgetController

_pipelinesFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'smart_pipelines')
_calibrationsFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'calibrations')
_stateFilePath = os.path.join(dirtools.UserFileDirs.Config, 'smart_pipeline.json')
_editorPreferencesPath = os.path.join(dirtools.UserFileDirs.Config, 'smart_pipeline_editor.json')
_roisFolderPath = os.path.join(dirtools.UserFileDirs.Root, 'smart_pipelines', 'rois')
#: Seconds to wait for camera frames when segmenting before saying none come.
_SEGMENT_FRAMES_TIMEOUT_S = 5.0
#: An armed pipeline that gets no camera frame for this long disarms, with the reason.
_FRAMES_WATCHDOG_S = 10.0

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
        self._rois = None             # the RoiSet the user controls (see the ROIs section)
        self._roiSelection = set()
        self._collect = None          # frames being averaged for a segmentation
        self._roiFile = None          # the file the running pipeline reads its ROIs from
        self._roiStep = None          # (step id, generation) of the running pipeline's roi-dff step
        self._counters = {}           # what the run did: triggers, scans, skips (and why), the last event
        self._stopReason = None       # why the run was disarmed by the panel (watchdog), shown
        self._watch = {}              # what the watchdog compares against
        self._saveWarnings = []       # why an Experiment run could not save, shown in a dry run
        self._shownGeometry = {}      # detector -> the scan geometry the live view shows (axes), or None
        self._marksGeometry = None    # the geometry the overlay's marks belong to
        self._parameters = []         # the loaded pipeline's exposed parameters (ExposedParameter)
        self._parameterValues = {}    # name -> value set in the panel, where it differs from the file
        self._savedParameters = {}    # pipeline file -> {name: value}: the panel's values, kept across starts
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
        for name, slot in (('sigParameterEdited', self.setParameter),
                           ('sigResetParametersClicked', self.resetParameters)):
            signal = getattr(self._widget, name, None)
            if signal is not None:
                signal.connect(slot)
        for name, slot in (('sigSegmentClicked', self.segmentRois), ('sigDrawClicked', self.drawRoi),
                           ('sigDeleteRoisClicked', self.deleteSelectedRois), ('sigClearRoisClicked', self.clearRois),
                           ('sigSaveRoisClicked', self.saveRoisAs), ('sigLoadRoisClicked', self.loadRoisFrom),
                           ('sigShowRoisToggled', lambda _shown: self._showRois()),
                           ('sigUseRoisToggled', lambda _use: self._saveState()),
                           ('sigRoiSelectionChanged', self._onRoiListSelection),
                           ('sigShapeDrawn', self.addDrawnRoi), ('sigRoiClicked', self._onRoiClicked)):
            signal = getattr(self._widget, name, None)
            if signal is not None:
                signal.connect(slot)
        self._commChannel.sigUpdateImage.connect(self._onPreviewImage)

        state = _loadState()
        saved = state.get('parameters')
        self._savedParameters = {str(k): dict(v) for k, v in saved.items() if isinstance(v, dict)} \
            if isinstance(saved, dict) else {}
        if hasattr(self._widget, 'setUseRois'):
            self._widget.setUseRois(state.get('use_rois', True))
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
        self._onDocumentForRois()
        self._onDocumentForParameters()
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
        """ The calibrations folder's transforms, and always ``identity``:
        the numbers 1:1 from any detector into the frame the action takes
        (a calibration file of that id replaces it). """
        from imswitch.imcommon.model.smartpipeline.transforms import OneToOne

        try:
            registry = load_calibrations(_calibrationsFolderPath)
        except CalibrationError as e:
            raise ValueError(str(e)) from e
        if 'identity' not in registry:
            registry.register(OneToOne())
        return registry

    def _catalog(self):
        """ The actions this setup offers: the device actions only for the
        devices it has. """
        from imswitch.imcontrol.model.smartpipeline.catalog import catalog

        return catalog(self._setupDevices())

    def _setupModeNames(self):
        service = self._smartModeService
        controller = getattr(service, 'setupModeController', None) if service is not None else None
        if controller is None:
            return None
        return [str(name) for name in (controller.listSetupModes() or [])]

    def _setupDevices(self):
        """ What the device actions may name: the flip mirrors (with their
        position names), the lasers (binary or not), the setup modes. A
        manager this setup does not have contributes nothing. """
        from imswitch.imcontrol.model.smartpipeline.catalog import SetupDevices

        mirrors, lasers = {}, {}
        manager = getattr(self._master, 'flipMirrorsManager', None)
        try:
            for name in (manager.getAllDeviceNames() if manager is not None and manager.hasDevices() else []):
                getNames = getattr(manager[name], 'get_state_names', None)
                try:
                    named = dict(getNames()) if getNames is not None else {0: '0', 1: '1'}
                except Exception:
                    named = {0: '0', 1: '1'}
                mirrors[str(name)] = tuple(str(named[k]) for k in sorted(named, key=int))
        except Exception:
            self.__logger.debug('Smart pipeline: the flip mirrors could not be listed', exc_info=True)
        manager = getattr(self._master, 'lasersManager', None)
        try:
            for name in (manager.getAllDeviceNames() if manager is not None else []):
                lasers[str(name)] = bool(getattr(manager[name], 'isBinary', False))
        except Exception:
            self.__logger.debug('Smart pipeline: the lasers could not be listed', exc_info=True)
        try:
            modes = self._setupModeNames()
        except Exception:
            modes = None
        return SetupDevices(flip_mirrors=mirrors, lasers=lasers,
                            setup_modes=tuple(modes) if modes is not None else None)

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
    # The exposed parameters                                              #
    # ------------------------------------------------------------------ #
    #: What the Parameters box says when nothing else needs saying.
    PARAMETERS_NOTE = ('Applies from the next frame while armed; a start-up setting (the ROI mask, the '
                       'baseline length) at the next arm.')

    def _onDocumentForParameters(self):
        """ A (re)loaded pipeline: its exposed parameters, with the values the
        panel kept for this file (a kept value its parameter no longer takes
        is dropped, with a note). """
        from imswitch.imcontrol.model.smartpipeline.parameters import checked_value, exposed_parameters

        self._parameters, self._parameterValues = [], {}
        document = self._document
        dropped = []
        if document is not None:
            registry = self._improcessRegistry() if self._needsImprocess(document) else None
            try:
                self._parameters = exposed_parameters(document.workflow, registry=registry, actions=self._catalog())
            except Exception:
                self.__logger.warning('Smart pipeline: the exposed parameters could not be read', exc_info=True)
            kept = dict(self._savedParameters.get(self._path or '', {}))
            for parameter in self._parameters:
                if parameter.name not in kept or not parameter.usable:
                    continue
                try:
                    value = checked_value(parameter, kept[parameter.name])
                except ValueError as e:
                    dropped.append(f'{parameter.label}: {e}')
                    continue
                if value != parameter.file_value:
                    self._parameterValues[parameter.name] = value
        if dropped:
            self._showParameters('Back to the file\'s value: ' + '; '.join(dropped), 'warn')
            self._rememberParameters()
        else:
            self._showParameters()

    def _parameterRows(self):
        rows = []
        for parameter in self._parameters:
            spec = parameter.spec
            rows.append({
                'name': parameter.name, 'label': parameter.label, 'help': parameter.help,
                'type': spec.type if spec is not None else 'text', 'unit': parameter.unit,
                'min': spec.min if spec is not None else None, 'max': spec.max if spec is not None else None,
                'decimals': spec.decimals if spec is not None else None,
                'step': spec.step if spec is not None else None,
                'options': list(spec.options) if spec is not None else [],
                'value': self._parameterValues.get(parameter.name, parameter.file_value),
                'file_value': parameter.file_value, 'problem': parameter.problem,
            })
        return rows

    def _showParameters(self, note='', level='info'):
        if not hasattr(self._widget, 'setParameters'):
            return
        self._widget.setParameters(self._parameterRows())
        self._widget.setParametersNote(note or (self.PARAMETERS_NOTE if self._parameters else ''), level)
        self._widget.setResetParametersEnabled(bool(self._parameterValues))

    def _rememberParameters(self):
        if not self._path:
            return
        if self._parameterValues:
            self._savedParameters[self._path] = dict(self._parameterValues)
        else:
            self._savedParameters.pop(self._path, None)
        self._saveState()

    def parameterValues(self):
        """ {name: value} the next arm uses: the file's, with the panel's over them. """
        return {p.name: self._parameterValues.get(p.name, p.file_value) for p in self._parameters if p.usable}

    def setParameter(self, name, value):
        """ An exposed parameter set in the panel: checked, kept for this
        pipeline file, used at the next arm and -- while armed -- applied to
        the running pipeline when that is safe. Returns whether it was taken. """
        from imswitch.imcontrol.model.smartpipeline.parameters import checked_value

        parameter = next((p for p in self._parameters if p.name == name), None)
        if parameter is None or not parameter.usable:
            return False
        current = self._parameterValues.get(name, parameter.file_value)
        try:
            value = checked_value(parameter, value)
        except ValueError as e:
            self._widget.setParameterValue(name, current)
            self._widget.setParametersNote(f'{parameter.label}: {e}', 'error')
            return False
        if value == parameter.file_value:
            self._parameterValues.pop(name, None)
        else:
            self._parameterValues[name] = value
        self._rememberParameters()
        self._widget.markParameter(name, value, parameter.file_value)
        self._widget.setResetParametersEnabled(bool(self._parameterValues))
        if value != current:
            note, level = self._applyParametersLive({name: value})
            self._widget.setParametersNote(f'{parameter.label} = {value!r}: {note}', level)
        return True

    def resetParameters(self):
        """ Every exposed parameter back to the file's value (also in a running pipeline). """
        changed = {name: p.file_value for p in self._parameters
                   for name in [p.name] if name in self._parameterValues and p.usable}
        self._parameterValues = {}
        self._rememberParameters()
        note, level = self._applyParametersLive(changed) if changed else ('', 'info')
        self._showParameters(f'Back to the file\'s values: {note}' if changed else '', level)

    def _applyParametersLive(self, values):
        """ Applies ``values`` to the running pipeline when one is armed and
        that is safe; (what happened, level) for the panel. """
        from imswitch.imcontrol.model.smartpipeline import EngineError
        from imswitch.imcontrol.model.smartpipeline.parameters import changes_for, live_refusal

        run = self._run
        if run is None or run.state not in (ARMED, RUNNING):
            return 'used when the pipeline is armed.', 'info'
        changes = changes_for(self._parameters, values)
        refusal = live_refusal(run.engine.workflow, changes, run.actions)
        if refusal:
            return f'kept for the next arm ({refusal}).', 'warn'
        try:
            run.engine.set_params(changes, why='set in the panel')
        except EngineError as e:
            return f'not applied now ({e}); used at the next arm.', 'warn'
        return 'applied to the running pipeline from the next frame.', 'info'

    def _documentWithParameters(self, document):
        """ The document to arm, with the panel's parameter values written into
        their steps (a copy; the file is not changed). """
        from imswitch.imcontrol.model.smartpipeline.parameters import apply_changes, changes_for

        if not self._parameterValues:
            return document
        copy = SmartPipelineDocument.from_dict(document.to_dict())
        apply_changes(copy.workflow, changes_for(self._parameters, self._parameterValues))
        return copy

    # ------------------------------------------------------------------ #
    # Arm / stop                                                          #
    # ------------------------------------------------------------------ #
    def arm(self):
        if self._run is not None and self._run.state in (ARMED, RUNNING, STOPPING):
            return
        if self._document is None or self._issues:
            self._updateButtons()
            return
        document = self._documentWithParameters(self._document)
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
        saving = recorder is not None and mode == RunMode.EXPERIMENT and self._savesScans(document) \
            and not saveProblems
        document, roiProblems = self._documentWithRois(document, recorder.folder if saving else None)
        if roiProblems:
            self._widget.setIssues([f'not armed: {problem}' for problem in roiProblems])
            self._widget.setStatus('not armed')
            return
        from imswitch.imcontrol.model.smartpipeline.preflight import errors

        checks = self._preflight(document, mode, recorder, saveProblems)
        self._widget.setPreflight([(c.level, f'{c.topic}: {c.text}') for c in checks])
        if errors(checks):
            self._widget.setIssues([f'not armed: {c.topic}: {c.text}' for c in errors(checks)])
            self._widget.setStatus('not armed')
            return
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
                               logger=self.__logger, scan=scan, devices=self._setupDevices(),
                               controllers=self._commChannel.controllerRegistry, apply_mode=self._applyMode,
                               setup_modes=lambda: self._marshal(self._setupModeNames) or [])
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
        self._counters = {'scans': 0, 'skipped': {}, 'would': 0, 'triggers': 0, 'last': '', 'scanning': None}
        self._stopReason = None
        self._watch = self._watchBaseline(document)
        for check in checks:
            log.append(0, time.monotonic(), 'preflight', check.level, topic=check.topic, text=check.text)
        if self._parameters:
            log.append(0, time.monotonic(), 'pipeline', 'parameters', values=self.parameterValues(),
                       changed=sorted(self._parameterValues))
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
    # The ROIs                                                            #
    # ------------------------------------------------------------------ #
    def _camera(self):
        document = self._document
        source = getattr(document, 'source', None) if document is not None else None
        return source.detector if isinstance(source, Live) and source.detector else None

    def _roiDffSteps(self, document=None):
        """ The steps the ROI set is for (roi-dff measuring a segmented or a
        file mask); none for a pipeline that needs no ROIs (a whole-frame
        threshold, a processor that finds its own points, roi-dff reading
        labels another step makes). """
        from imswitch.imcontrol.model.smartpipeline.roi_set import roi_set_steps

        document = document or self._document
        if document is None:
            return []
        return roi_set_steps(document.workflow)

    def _cameraIdentity(self, camera):
        from imswitch.imcontrol.model.smartpipeline.live_source import frame_identity_of

        try:
            return frame_identity_of(self._master.detectorsManager[camera]).to_dict()
        except Exception:
            return None

    def _displayTransform(self, camera):
        from imswitch.imcontrol.controller.display_transform import display_transform_from_properties

        info = (getattr(self._setupInfo, 'detectors', None) or {}).get(camera)
        return display_transform_from_properties(getattr(info, 'managerProperties', None))

    def _roisAutosavePath(self, camera):
        return os.path.join(_roisFolderPath, f'{camera}_current')

    def _onDocumentForRois(self):
        """ A (re)loaded pipeline: its segmentation settings fill the form,
        the last ROIs of its camera come back. """
        camera = self._camera()
        steps = self._roiDffSteps()
        if not hasattr(self._widget, 'setRois'):
            return
        if hasattr(self._widget, 'setRoisVisible'):
            # A pipeline that measures no ROI set shows no ROIs box (and arming asks for none).
            self._widget.setRoisVisible(bool(steps))
        enabled = camera is not None and bool(steps)
        self._widget.setRoiControlsEnabled(enabled, '' if enabled else
                                           'The pipeline has no live camera and roi-dff step to give ROIs to')
        if steps:
            params = dict(steps[0].params)
            params.setdefault('reference_frames', 1)
            self._widget.setSegmentationParameters(params)
        if camera is not None and (self._rois is None or self._rois.detector != camera):
            self._rois = None
            path = self._roisAutosavePath(camera) + '.tif'
            if os.path.isfile(path):
                try:
                    from imswitch.imcontrol.model.smartpipeline.roi_set import RoiSet

                    self._rois = RoiSet.load(path)
                except Exception:
                    self.__logger.warning('Smart pipeline: the last ROIs could not be read', exc_info=True)
        self._roiSelection = set()
        self._roisChanged(autosave=False, push=False)

    def _roisChanged(self, autosave=True, push=True):
        """ The ROI set changed: list, overlay, autosave, the running pipeline
        (``push``; a selection is not a change of the ROIs). """
        rois = self._rois
        if hasattr(self._widget, 'setRois'):
            if rois is None or not len(rois):
                summary = 'No ROIs yet: segment the camera image or draw them.'
                if self._roiDffSteps() and not self._widget.isUseRois():
                    summary += ' (The pipeline segments at arm.)'
                self._widget.setRois([], summary)
            else:
                table = rois.table()
                drawn = sum(1 for row in table if row['source'] == 'drawn')
                summary = (f'{len(table)} ROIs ({len(table) - drawn} segmented, {drawn} drawn) on '
                           f'{rois.shape[1]}x{rois.shape[0]} px of {rois.detector or "?"}')
                problems = rois.grid_problems(self._cameraIdentity(rois.detector)) if rois.detector else []
                if problems:
                    summary += ' -- ' + problems[0]
                self._widget.setRois(table, summary, self._roiSelection)
        if autosave and rois is not None and rois.detector:
            try:
                rois.save(self._roisAutosavePath(rois.detector))
            except Exception:
                self.__logger.warning('Smart pipeline: the ROIs could not be kept for the next start', exc_info=True)
        self._showRois()
        if push:
            self._pushRoisToRun()

    def _showRois(self):
        rois = self._rois
        if not hasattr(self._widget, 'showRoiOverlay'):
            return
        if rois is None or not len(rois) or not self._widget.isShowRois() or not self._roiDffSteps():
            self._widget.hideRoiOverlay()
            return
        from imswitch.imcontrol.controller.display_transform import apply_display_transform
        from imswitch.imcontrol.model.smartpipeline.roi_set import detector_points_to_display

        transform = self._displayTransform(rois.detector)
        outline, _ = apply_display_transform(rois.outline(), None, transform)
        table = rois.table()
        centres = detector_points_to_display([row['centroid'] for row in table], rois.shape, transform)
        self._widget.showRoiOverlay(rois.detector, outline, centres, [row['id'] for row in table],
                                    self._roiSelection)

    def _ensureRois(self):
        """ The ROI set to change, made empty on the camera's grid when there is none. """
        from imswitch.imcontrol.model.smartpipeline.roi_set import RoiSet

        camera = self._camera()
        if camera is None:
            raise ValueError('the pipeline has no live camera to put ROIs on')
        identity = self._cameraIdentity(camera)
        if self._rois is None or self._rois.detector != camera:
            if identity is None:
                raise ValueError(f'no camera {camera!r} in this setup')
            self._rois = RoiSet.empty(identity['shape'], detector=camera, identity=identity,
                                      stage_position_um=self._stagePosition())
        return self._rois

    def segmentRois(self):
        """ Average the next N frames of the camera and segment them with the
        form's settings (in the background); drawn ROIs are kept. """
        camera = self._camera()
        if camera is None or self._collect is not None:
            return
        params = self._widget.getSegmentationParameters()
        self._collect = {'camera': camera, 'need': max(1, int(params.get('reference_frames', 1))), 'n': 0,
                         'sum': None, 'params': params, 'started': time.monotonic()}
        self._widget.setSegmenting(True)
        QtCore.QTimer.singleShot(int(_SEGMENT_FRAMES_TIMEOUT_S * 1000), self._segmentTimedOut)

    def _segmentTimedOut(self):
        collect = self._collect
        if collect is None or time.monotonic() - collect['started'] < _SEGMENT_FRAMES_TIMEOUT_S - 0.1:
            return
        self._collect = None
        self._widget.setSegmenting(False)
        self._widget.setIssues([f'No frames from {collect["camera"]} in {_SEGMENT_FRAMES_TIMEOUT_S:g} s '
                                f'({collect["n"]} of {collect["need"]}): start its live view, then Segment now.'])

    def _onPreviewImage(self, detectorName, image, init=False, scale=None, isCurrentDetector=False):
        """ GUI thread, every frame: nothing unless a segmentation is collecting frames. """
        collect = self._collect
        if collect is None or str(detectorName) != collect['camera']:
            return
        array = np.asarray(image, dtype=np.float64)
        if array.ndim != 2:
            return
        if collect['sum'] is not None and collect['sum'].shape != array.shape:
            collect['sum'], collect['n'] = None, 0
        collect['sum'] = array.copy() if collect['sum'] is None else collect['sum'] + array
        collect['n'] += 1
        if collect['n'] < collect['need']:
            return
        self._collect = None
        average = collect['sum'] / collect['n']
        params = collect['params']

        def work():
            from imswitch.imcontrol.model.smartpipeline.roi_set import segment

            try:
                labels, error = segment(average, params), None
            except Exception as e:
                labels, error = None, f'{type(e).__name__}: {e}'
            self._invokeOnControllerThreadIfNeeded(lambda: self._segmented(collect['camera'], labels, params, error))

        threading.Thread(target=work, name='smart-pipeline-segment', daemon=True).start()

    def _segmented(self, camera, labels, params, error):
        self._widget.setSegmenting(False)
        if error is not None:
            self._widget.setIssues([f'Segmentation failed: {error}'])
            return
        try:
            rois = self._ensureRois()
            if rois.shape != labels.shape:
                rois = self._rois = None
                rois = self._ensureRois()
            rois.stage_position_um = self._stagePosition()
            new = rois.replace_segmented(labels, params)
        except ValueError as e:
            self._widget.setIssues([str(e)])
            return
        self._widget.setIssues([] if new else ['The segmentation found no ROI: lower the threshold or min area, '
                                               'or draw the ROIs.'])
        self._roisChanged()

    def drawRoi(self, kind):
        try:
            self._ensureRois()
        except ValueError as e:
            self._widget.setIssues([str(e)])
            return
        self._showRois()
        self._widget.startDrawingRoi(kind)

    def addDrawnRoi(self, kind, vertices):
        """ A shape drawn on the live view (displayed pixels) becomes a ROI. """
        from imswitch.imcontrol.model.smartpipeline.roi_set import display_polygon_to_detector, shape_polygon

        try:
            rois = self._ensureRois()
            polygon = shape_polygon(kind, vertices)
        except ValueError as e:
            self._widget.setIssues([str(e)])
            return None
        detector = display_polygon_to_detector(polygon, rois.shape, self._displayTransform(rois.detector))
        new = rois.add_polygon(detector, kind=kind)
        if new is None:
            self._widget.setIssues(['The drawn shape holds no pixel: draw it larger.'])
            return None
        self._roiSelection = {new}
        self._roisChanged()
        return new

    def deleteSelectedRois(self):
        if self._rois is None:
            return
        removed = self._rois.remove(self._roiSelection or self._widget.getSelectedRois())
        self._roiSelection -= set(removed)
        if removed:
            self._roisChanged()

    def clearRois(self):
        if self._rois is not None and len(self._rois):
            self._rois.clear()
            self._roiSelection = set()
            self._roisChanged()

    def _onRoiListSelection(self, ids):
        self._roiSelection = set(int(i) for i in ids)
        self._showRois()

    def _onRoiClicked(self, roiId):
        if not roiId:
            return
        self._roiSelection ^= {int(roiId)}
        self._roisChanged(autosave=False, push=False)

    def saveRoisAs(self, path=None):
        if self._rois is None or not len(self._rois):
            return None
        os.makedirs(_roisFolderPath, exist_ok=True)
        path = path or self._widget.askForRoisPath(_roisFolderPath, True)
        if not path:
            return None
        return self._rois.save(path)

    def loadRoisFrom(self, path=None):
        from imswitch.imcontrol.model.smartpipeline.roi_set import RoiSet

        camera = self._camera()
        os.makedirs(_roisFolderPath, exist_ok=True)
        path = path or self._widget.askForRoisPath(_roisFolderPath, False)
        if not path:
            return None
        try:
            rois = RoiSet.load(path)
        except Exception as e:
            self._widget.setIssues([f'Cannot read the ROIs: {e}'])
            return None
        if camera is not None:
            rois.detector = rois.detector or camera
            if rois.identity is None:
                rois.identity = self._cameraIdentity(camera)
            problems = rois.grid_problems(self._cameraIdentity(camera))
            self._widget.setIssues(problems)
        self._rois, self._roiSelection = rois, set()
        self._roisChanged()
        return rois

    def _documentWithRois(self, document, folder):
        """ The document to arm: with the ROI set in use, its roi-dff step
        reads exactly these ROIs (``mask_source: file``, a new generation),
        saved where the run's records go (or with the ROIs kept for the next
        start). Returns (document, problems). """
        steps = self._roiDffSteps(document)
        rois = self._rois
        self._roiFile = self._roiStep = None
        if not steps or rois is None or not len(rois) or not self._widget.isUseRois():
            return document, []
        camera = self._camera()
        if rois.detector and camera and rois.detector != camera:
            return document, [f'the ROIs are of {rois.detector}, the pipeline watches {camera}: draw or segment '
                              f'them on {camera}, or uncheck "Use these ROIs when arming"']
        problems = rois.grid_problems(self._cameraIdentity(camera))
        if problems:
            return document, problems
        copy = SmartPipelineDocument.from_dict(document.to_dict())
        step = self._roiDffSteps(copy)[0]
        path = self._writeRoisForRun(folder)
        generation = int(step.params.get('generation', 0) or 0) + 1
        step.params.update(mask_source='file', labels_path=path, generation=generation)
        self._roiFile, self._roiStep = (folder, path), (step.id, generation)
        self._pushedVersion = rois.version
        return copy, []

    def _writeRoisForRun(self, folder):
        rois = self._rois
        if folder:
            return rois.save(os.path.join(folder, f'rois_R{rois.region}_v{rois.version}'))
        return rois.save(os.path.join(_roisFolderPath, f'{rois.detector}_armed'))

    def _pushRoisToRun(self):
        """ The ROIs changed while the pipeline runs on them: it reads the new
        set from the next frame on (a new generation: baselines start over). """
        run, step = self._run, self._roiStep
        if run is None or step is None or run.state not in (ARMED, RUNNING) or self._rois is None:
            return
        if not len(self._rois) or self._rois.version == getattr(self, '_pushedVersion', None):
            return
        self._pushedVersion = self._rois.version
        folder = self._roiFile[0] if self._roiFile else None
        path = self._writeRoisForRun(folder)
        stepId, generation = step
        generation += 1
        engine = run.engine
        with engine._lock:
            for candidate in engine.workflow.steps:
                if candidate.id == stepId:
                    candidate.params.update(mask_source='file', labels_path=path, generation=generation)
        self._roiStep = (stepId, generation)
        run.log.append(engine._tick, time.monotonic(), stepId, 'rois-changed', rois=len(self._rois),
                       version=self._rois.version, file=path, why='new ROI set: the baselines start over')

    # ------------------------------------------------------------------ #
    # Preflight: what arming checks                                       #
    # ------------------------------------------------------------------ #
    def _preflight(self, document, mode, recorder, saveProblems):
        """ Every check of the arm, as a list the panel shows and the log keeps;
        an ``error`` refuses the arm. Runs on the GUI thread, touches no device. """
        from imswitch.imcommon.model.smartpipeline import FrameIdentity, Gate, Transform
        from imswitch.imcontrol.model.smartpipeline.boxes import transform_record
        from imswitch.imcontrol.model.smartpipeline.calibrations import identity_problems
        from imswitch.imcontrol.model.smartpipeline.catalog import RUN_SCAN
        from imswitch.imcontrol.model.smartpipeline.live_actions import RunScanAdapter, scan_panel_busy
        from imswitch.imcontrol.model.smartpipeline.preflight import (
            ERROR, OK, WARN, Check, classify_rois, roi_checks, scan_time_check,
        )

        experiment = mode == RunMode.EXPERIMENT
        checks = []
        camera = self._camera()
        identity = self._cameraIdentity(camera) if camera else None
        if camera is not None:
            if identity is None:
                checks.append(Check(ERROR, 'Camera', f'no detector {camera!r} in this setup'))
            else:
                checks.append(Check(OK, 'Camera', f'{camera}: {identity["shape"][1]}x{identity["shape"][0]} px, '
                                                  f'binning {identity["binning"][0]}, ROI offset (row, col) '
                                                  f'{tuple(identity["offset"])}'))
        try:
            transforms = self._transforms()
        except ValueError as e:
            transforms = None
            checks.append(Check(ERROR, 'Calibrations', str(e)))
        transform = None
        for step in document.workflow.steps:
            if not isinstance(step, Transform) or transforms is None:
                continue
            found = transforms.get(step.transform)
            if found is None:
                checks.append(Check(ERROR, 'Calibration', f'{step.transform!r} not found in calibrations/'))
                continue
            transform = transform or found
            record = transform_record(found)
            residual = record.get('residual')
            text = f'{step.transform!r} ({record.get("type") or "?"})' + (
                f', residual {residual}' if residual is not None else ', no residual recorded')
            problems = identity_problems(found, FrameIdentity.from_dict(identity) if identity else None)
            if problems:
                recorded = getattr(found, 'calibrated_on', None) or {}
                checks.append(Check(ERROR, 'Calibration', '; '.join(problems) + ': set the camera ROI/binning to '
                                    f'{recorded.get("shape")} / binning {recorded.get("binning")} / offset '
                                    f'{recorded.get("offset")}, or recalibrate (Hardware > Camera -> scan '
                                    'calibration...)'))
            else:
                checks.append(Check(OK, 'Calibration', text + (', made on this camera grid'
                                                               if getattr(found, 'calibrated_on', None) else '')))
        scanSteps = self._scanSteps(document)
        registry = self._commChannel.controllerRegistry() or {}
        classified = []
        for step in scanSteps:
            params = {**RUN_SCAN.params, **step.params}
            sourceName = str(params.get('source') or 'Scan')
            panel = registry.get(sourceName)
            if panel is None:
                checks.append(Check(ERROR, 'Scan', f'no scan panel {sourceName!r} in this setup'))
                continue
            advanced = not hasattr(panel, 'applyAcquisitionPlan')
            busy = scan_panel_busy(panel)
            if busy:
                checks.append(Check(ERROR if experiment else WARN, 'Scan',
                                    f'the {sourceName!r} panel is busy ({busy}): stop its scan first'))
            else:
                checks.append(Check(OK, 'Scan', f'{sourceName!r} panel ({"Advanced" if advanced else "simple"}), '
                                                'idle'))
            checks.extend(self._laserChecks(params, experiment))
            rois = self._rois if (self._rois is not None and len(self._rois) and self._roiDffSteps(document)
                                  and self._widget.isUseRois()) else None
            widget = getattr(panel, '_widget', None)
            if rois is not None and transform is not None and advanced and widget is not None:
                axes = [str(a) for a in (params.get('axes') or ['Y', 'X'])]
                try:
                    steps = {axis: (float(params.get('pixel_um') or 0.0) or float(widget.getScanStepSize(axis)))
                             for axis in axes[:2]}
                    adapter = RunScanAdapter(lambda: registry, None)
                    ranges = adapter._scanner_ranges(panel, axes[:2], params)
                    classified = classify_rois(rois.table(), transform, axes=axes, params=params, steps=steps,
                                               ranges=ranges)
                    checks.extend(roi_checks(classified))
                    dwell = float(params.get('dwell_s') or 0.0) or (
                        float(widget.getSeqTimePar()) if hasattr(widget, 'getSeqTimePar') else None)
                    timeCheck = scan_time_check(classified, dwell)
                    if timeCheck is not None:
                        checks.append(timeCheck)
                except Exception as e:
                    checks.append(Check(WARN, 'ROIs', f'could not plan their scans: {type(e).__name__}: {e}'))
            elif rois is None and self._roiDffSteps(document):
                checks.append(Check(WARN, 'ROIs', 'the pipeline segments the cells itself at arm: press Segment '
                                                  'now (ROIs box) to see them, and their scans, before arming'))
        if scanSteps:
            detectors = list(getattr(recorder, 'detectors', ()) or ())
            checks.append(Check(OK if detectors else (ERROR if experiment else WARN), 'Scan detectors',
                                ', '.join(detectors) or 'none (no APD/PMT in this setup)'))
        for step in document.workflow.steps:
            if isinstance(step, Gate) and step.rule == 'min-interval':
                checks.append(Check(OK, 'Interval', f'{step.id}: at most one scan per '
                                                    f'{float(step.params.get("seconds", 0)):g} s'))
            elif isinstance(step, Gate) and step.rule == 'count-limit':
                checks.append(Check(OK, 'Budget', f'{step.id}: at most {int(step.params.get("count", 0))} scans '
                                                  'per run (a skipped trigger does not count)'))
        if scanSteps and self._savesScans(document):
            if saveProblems:
                for problem in saveProblems:
                    checks.append(Check(ERROR if experiment else WARN, 'Saving',
                                        f'the triggered scans could not be saved -- {problem}'))
            elif recorder is not None:
                import shutil

                free = None
                probe = recorder.folder
                while probe and not os.path.isdir(probe):
                    probe = os.path.dirname(probe)
                try:
                    free = shutil.disk_usage(probe).free if probe else None
                except OSError:
                    pass
                where = recorder.folder if experiment else f'(Experiment would save to) {recorder.folder}'
                checks.append(Check(OK, 'Saving', where + (f', {free / 1e9:.1f} GB free' if free else '')))
        return checks

    def _laserChecks(self, params, experiment):
        from imswitch.imcontrol.model.smartpipeline.preflight import ERROR, OK, WARN, Check

        checks = []
        lasers = [str(name) for name in (params.get('lasers') or [])]
        manager = getattr(self._master, 'lasersManager', None)
        names = []
        try:
            names = list(manager.getAllDeviceNames()) if manager is not None else []
        except Exception:
            names = []
        controller = (self._commChannel.controllerRegistry() or {}).get('Laser')
        if not lasers:
            checks.append(Check(WARN, 'Lasers', 'the scan names no laser: it fires what the panel\'s first '
                                                'line step holds'))
        for name in lasers:
            if manager is not None and name not in names:
                checks.append(Check(ERROR, 'Laser', f'{name!r} is not a laser of this setup (have: '
                                                    f'{", ".join(names) or "none"}): fix the scan step\'s lasers'))
                continue
            device = None
            try:
                device = manager[name] if manager is not None else None
            except Exception:
                pass
            usable = getattr(device, 'isUsable', True)
            mode = str(getattr(getattr(device, 'runtimeMode', None), 'value', '') or '')
            if mode == 'absent' or usable is False:
                checks.append(Check(ERROR if experiment else WARN, 'Laser',
                                    f'{name!r} is {"absent" if mode == "absent" else "not usable"}: reconnect it '
                                    '(Hardware) before arming'))
                continue
            value = None
            if controller is not None and hasattr(controller, 'getLaserValue'):
                try:
                    value = controller.getLaserValue(name)
                except Exception:
                    value = None
            units = getattr(device, 'valueUnits', '') or ''
            text = f'{name!r}' + (f' at {value:g} {units}'.rstrip() if isinstance(value, (int, float)) else '')
            if isinstance(value, (int, float)) and value <= 0:
                checks.append(Check(WARN, 'Laser', text + ': its power is 0, the scan will be dark'))
            else:
                checks.append(Check(OK, 'Laser', text + (' (mock)' if mode == 'mock' else '')))
        return checks

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
        try:
            positioners = list(self._master.positionersManager)
        except Exception:
            return found
        for item in positioners:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            name, positioner = item
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

    def _count(self, entry):
        """ The counters and the last event, from the run's log (any thread). """
        counters = self._counters
        if not counters:
            return
        detail = entry.detail
        if entry.event in ('started', 'would') and detail.get('action') == 'run-scan':
            counters['triggers'] += 1
            row = ((detail.get('inputs') or [{}])[0] or {}).get('first_row') or {}
            counters['scanning'] = row.get('label')
            if entry.event == 'would':
                counters['would'] += 1
                counters['last'] = str(detail.get('message') or 'would scan')
        elif entry.event == 'done' and isinstance(detail.get('result'), dict) and 'scans' in detail['result']:
            result = detail['result']
            counters['scans'] += int(result.get('scans') or 0)
            for skip in result.get('skipped') or []:
                reason = str(skip.get('reason') or '?')
                short = ('outside the scan range' if 'range' in reason else 'panel busy' if 'busy' in reason
                         else reason[:40])
                counters['skipped'][short] = counters['skipped'].get(short, 0) + 1
            label = (result.get('trigger') or {}).get('label')
            if result.get('scans'):
                counters['last'] = f'scanned ROI {label}' + (f' -> {os.path.basename(result["saved"][0])}'
                                                             if result.get('saved') else '')
            elif result.get('skipped'):
                counters['last'] = f'skipped: {result["skipped"][0].get("reason")}'
            counters['scanning'] = None
        elif entry.event == 'failed':
            counters['last'] = f'{entry.step} failed: {detail.get("error")}'
            counters['scanning'] = None

    def _onLogEntry(self, entry):
        text = str(entry)
        try:
            self._count(entry)
        except Exception:
            pass
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
                return (f'no setup mode {modeName!r} (have: {", ".join(available) or "none"}). Save one with '
                        f'that name in the Setup modes panel (More > Save as...), or set the role to '
                        f'"(unchanged)" in the pipeline to leave the microscope as it is')
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
    def _runState(self, run):
        """ (kind, text) of the state indicator. """
        if run is None:
            return 'idle', 'Idle'
        if run.state in (STOPPED, FAILED):
            reason = self._stopReason or run.error or 'by the user'
            return ('failed' if (run.error or self._stopReason) else 'stopped'), f'Stopped: {reason}'
        if run.state == STOPPING:
            return 'stopped', 'Stopping…'
        engine = run.engine
        for stepId, state in list(engine.step_states.items()):
            if state and state != 'running':
                return 'warming', f'Warming up: {state}' if state.startswith(('warming', 'collecting')) else state
        mode = run.mode
        counters = self._counters or {}
        if engine.busy and mode == RunMode.EXPERIMENT:
            label = counters.get('scanning')
            return 'scanning', f'Scanning ROI {label}' if label is not None else 'Scanning'
        if mode == RunMode.VALIDATE:
            return 'dry', 'Armed — dry run (nothing scans)'
        if mode == RunMode.VISUALIZE:
            return 'dry', 'Watching — actions skipped'
        return 'armed', 'ARMED — will scan'

    def _countersText(self, status):
        counters = self._counters or {}
        parts = [f'frames {status["ticks"]}', f'triggers {counters.get("triggers", 0)}']
        if status['mode'] == 'experiment':
            parts.append(f'scans {counters.get("scans", 0)}')
        skipped = counters.get('skipped') or {}
        if skipped:
            parts.append(f'skipped {sum(skipped.values())} (' + ', '.join(f'{k} {v}' for k, v in skipped.items())
                         + ')')
        if counters.get('last'):
            parts.append(f'last: {counters["last"]}')
        return ' · '.join(parts)

    def _watchBaseline(self, document):
        """ What the watchdog compares against: the calibration files' times,
        the lasers the scans fire. """
        from imswitch.imcommon.model.smartpipeline import Transform
        from imswitch.imcontrol.model.smartpipeline.catalog import RUN_SCAN

        files = {}
        for step in document.workflow.steps:
            if isinstance(step, Transform):
                path = os.path.join(_calibrationsFolderPath, f'{step.transform}.json')
                try:
                    files[path] = os.path.getmtime(path)
                except OSError:
                    pass
        lasers = sorted({str(name) for step in self._scanSteps(document)
                         for name in ({**RUN_SCAN.params, **step.params}.get('lasers') or [])})
        return {'files': files, 'lasers': lasers, 'since': time.monotonic()}

    def _watchdog(self, run):
        """ A reason to disarm now, or None: no camera frames for a while, a
        calibration changed on disk, a laser the scans fire gone absent. """
        source = self._source
        watch = self._watch or {}
        last = getattr(source, 'last_frame_t', None) or watch.get('since')
        if last is not None and time.monotonic() - last > _FRAMES_WATCHDOG_S:
            return (f'no frame from {getattr(source, "detector", "the camera")} for {_FRAMES_WATCHDOG_S:g} s '
                    '(is the camera streaming?)')
        for path, mtime in (watch.get('files') or {}).items():
            try:
                changed = os.path.getmtime(path) != mtime
            except OSError:
                changed = True
            if changed:
                return f'the calibration {os.path.basename(path)} changed on disk: re-arm to use it'
        manager = getattr(self._master, 'lasersManager', None)
        for name in watch.get('lasers') or []:
            try:
                device = manager[name]
            except Exception:
                continue
            mode = str(getattr(getattr(device, 'runtimeMode', None), 'value', '') or '')
            if mode == 'absent' or getattr(device, 'isUsable', True) is False:
                return f'the laser {name!r} is no longer usable'
        return None

    def _refreshStatus(self):
        run = self._run
        if hasattr(self._widget, 'setRunState'):
            kind, text = self._runState(run)
            self._widget.setRunState(kind, text, self._countersText(run.status()) if run is not None else '')
        if run is None:
            self._widget.setStatus('')
            return
        if run.state == RUNNING and self._stopReason is None:
            reason = self._watchdog(run)
            if reason:
                self._stopReason = reason
                run.log.append(run.engine._tick, time.monotonic(), 'pipeline', 'disarmed', why=reason)
                run.stop()
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
                json.dump({'path': self._path, 'mode': self._widget.getMode(),
                           'use_rois': bool(getattr(self._widget, 'isUseRois', lambda: True)()),
                           'parameters': self._savedParameters}, file, indent=2, default=str)
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
