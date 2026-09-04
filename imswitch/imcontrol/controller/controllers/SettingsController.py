import contextlib
from dataclasses import dataclass
from typing import Any, List, Tuple, Dict

import numpy as np

from imswitch.imcommon.model import APIExport, RestoreWarning
from imswitch.imcontrol.model import configfiletools, getWidgetStatePersistence
from imswitch.imcontrol.view import guitools as guitools
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


@dataclass
class SettingsControllerParams:
    model: Any
    binning: Any
    frameMode: Any
    x0: Any
    y0: Any
    width: Any
    height: Any
    applyROI: Any
    newROI: Any
    abortROI: Any
    saveMode: Any
    deleteMode: Any
    allDetectorsFrame: Any


class SettingsController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to SettingsWidget."""
    
    # StatefulComponentMixin attributes
    componentName = 'Settings'
    setupModeDisplayName = 'Detector'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'detector_settings'
    setupModeApplyPriority = SetupModeApplyPriority.DETECTOR_SETTINGS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settingAttr = False
        self.allParams = {}

        # Set while the widget is being refreshed FROM the detector, so the
        # param handlers below can tell a readback from a user edit. See
        # _writebackSuppressed().
        self._suppressWriteback = False

        if not self._master.detectorsManager.hasDevices():
            return

        # Set up detectors
        for dName, dManager in self._master.detectorsManager:
            if not dManager.forAcquisition:
                continue

            # Check if detector supports advanced property introspection
            supportsAdvancedProperties = hasattr(dManager, 'getAdvancedPropertyInfo')
            
            self._widget.addDetector(
                dName, dManager.model, dManager.parameters, dManager.actions,
                dManager.supportedBinnings, self._setupInfo.rois,
                supportsAdvancedProperties=supportsAdvancedProperties
            )
            
            # If advanced properties are supported, connect refresh signal and populate
            if supportsAdvancedProperties:
                advancedWidget = self._widget.getAdvancedWidget(dName)
                if advancedWidget:
                    # Connect refresh signal
                    advancedWidget.sigRefreshClicked.connect(
                        lambda checked=False, name=dName: self.refreshAdvancedProperties(name)
                    )
                    # Connect property change signal
                    advancedWidget.sigPropertyChangeRequested.connect(
                        lambda propName, value, name=dName: self.applyAdvancedProperty(name, propName, value)
                    )
                    # Initial population
                    self.refreshAdvancedProperties(dName)

        self.roiAdded = False
        self.initParameters()

        execOnAll = self._master.detectorsManager.execOnAll
        execOnAll(lambda c: (self.updateParamsFromDetector(detector=c)),
                  condition=lambda c: c.forAcquisition)
        execOnAll(lambda c: (self.adjustFrame(detector=c)),
                  condition=lambda c: c.forAcquisition)
        execOnAll(lambda c: (self.updateFrame(detector=c)),
                  condition=lambda c: c.forAcquisition)
        execOnAll(lambda c: (self.updateFrameActionButtons(detector=c)),
                  condition=lambda c: c.forAcquisition)

        self.detectorSwitched(self._master.detectorsManager.getCurrentDetectorName())

        self.updateSharedAttrs()

        # Connect CommunicationChannel signals
        self._commChannel.sigDetectorSwitched.connect(self.detectorSwitched)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)

        # Connect SettingsWidget signals
        self._widget.sigROIChanged.connect(self.ROIchanged)
        self._widget.sigDetectorChanged.connect(self.detectorSwitchClicked)
        self._widget.sigNextDetectorClicked.connect(self.detectorNextClicked)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Settings', self)

    def addROI(self):
        """ Adds the ROI to ImageWidget viewbox through the CommunicationChannel. """
        if not self.roiAdded:
            self._commChannel.sigAddItemToVb.emit(self._widget.getROIGraphicsItem())
            self.roiAdded = True

    def toggleROI(self, b, position=None, size=None):
        """ Show or hide ROI. """
        if b:
            self.addROI()
            self._widget.showROI(position, size)
        else:
            self._widget.hideROI()

    def initParameters(self):
        """ Take parameters from the detector Tree map. """
        for detectorName in self._master.detectorsManager.getAllDeviceNames():
            if self._master.detectorsManager[detectorName].forAcquisition:
                detectorTree = self._widget.trees[detectorName]
                framePar = detectorTree.p.param('Image frame')
                self.allParams[detectorName] = SettingsControllerParams(
                    model=detectorTree.p.param('Model'),
                    binning=framePar.param('Binning'),
                    frameMode=framePar.param('Mode'),
                    x0=framePar.param('X0'),
                    y0=framePar.param('Y0'),
                    width=framePar.param('Width'),
                    height=framePar.param('Height'),
                    applyROI=framePar.param('Apply'),
                    newROI=framePar.param('New ROI'),
                    abortROI=framePar.param('Abort ROI'),
                    saveMode=framePar.param('Save mode'),
                    deleteMode=framePar.param('Delete mode'),
                    allDetectorsFrame=framePar.param('Update all detectors')
                )

                params = self.allParams[detectorName]
                params.binning.sigValueChanged.connect(self.updateBinning)
                params.frameMode.sigValueChanged.connect(self.updateFrame)
                params.applyROI.sigActivated.connect(self.applyROIClicked)
                params.newROI.sigActivated.connect(self.updateFrame)
                params.abortROI.sigActivated.connect(self.abortROI)
                params.saveMode.sigActivated.connect(self.saveMode)
                params.deleteMode.sigActivated.connect(self.deleteMode)

                def syncFrameParamsWithoutUpdates(): self.syncFrameParams(False, False)
                params.x0.sigValueChanged.connect(syncFrameParamsWithoutUpdates)
                params.y0.sigValueChanged.connect(syncFrameParamsWithoutUpdates)
                params.width.sigValueChanged.connect(syncFrameParamsWithoutUpdates)
                params.height.sigValueChanged.connect(syncFrameParamsWithoutUpdates)
                params.allDetectorsFrame.sigValueChanged.connect(self.syncFrameParams)

        detectorsParameters = self._master.detectorsManager.execOnAll(
            lambda c: c.parameters, condition=lambda c: c.forAcquisition
        )
        for detectorName, detectorParameters in detectorsParameters.items():
            for parameterName, parameter in detectorParameters.items():
                paramInWidget = self._widget.trees[detectorName].p.param(parameter.group).param(
                    parameterName
                )
                paramInWidget.sigValueChanged.connect(
                    lambda _, value, detectorName=detectorName, parameterName=parameterName:
                    self.setDetectorParameter(detectorName, parameterName, value)
                )

        detectorsActions = self._master.detectorsManager.execOnAll(
            lambda c: c.actions, condition=lambda c: c.forAcquisition
        )
        for detectorName, detectorActions in detectorsActions.items():
            for actionName, action in detectorActions.items():
                paramInWidget = self._widget.trees[detectorName].p.param(action.group).param(
                    actionName
                )
                paramInWidget.sigActivated.connect(action.func)

    def adjustFrame(self, *, detector=None):
        """ Crop detector and adjust frame.

        Returns a list of ``(detectorName, error)`` for detectors that refused
        the ROI -- empty when everything applied. A refusal must not propagate:
        this runs from ``__init__``'s execOnAll, so an exception here would
        abort controller construction and take the whole application down with
        it. The frame fields are refreshed from hardware either way, so a
        refused ROI is visible rather than left displayed as if applied. """

        if detector is None:
            results = self.getDetectorManagerFrameExecFunc()(
                lambda c: self.adjustFrame(detector=c)
            )
            # execOnAll returns a dict of per-detector results; execOnCurrent
            # returns the single detector's result.
            if isinstance(results, dict):
                return [failure for result in results.values() for failure in (result or [])]
            return list(results or [])

        # Adjust frame
        params = self.allParams[detector.name]
        binning = int(params.binning.value())
        width = params.width.value()
        height = params.height.value()
        x0 = params.x0.value()
        y0 = params.y0.value()

        # Round to closest "divisable by 4" value.
        hpos = binning * x0
        vpos = binning * y0
        hsize = binning * width
        vsize = binning * height

        hmodulus = 4
        vmodulus = 4
        vpos = int(vmodulus * np.ceil(vpos / vmodulus))
        hpos = int(hmodulus * np.ceil(hpos / hmodulus))
        vsize = int(vmodulus * np.ceil(vsize / vmodulus))
        hsize = int(hmodulus * np.ceil(hsize / hmodulus))

        failures = []
        try:
            detector.crop(hpos, vpos, hsize, vsize)
        except Exception as e:
            # The detector refused the ROI outright (as opposed to snapping it
            # to a hardware step, which crop() reports by returning normally).
            # Fall through: the refresh below then shows the geometry the
            # detector really has instead of the ROI that was never applied.
            self._logger.error(f'Could not apply ROI to {detector.name}: {e}')
            failures.append((detector.name, str(e)))

        # Final shape values might differ from the user-specified one because of detector limitation
        # x128
        if detector.name == self._master.detectorsManager.getCurrentDetectorName():
            self._commChannel.sigAdjustFrame.emit(detector.shape)
            self._widget.hideROI()

        self.updateParamsFromDetector(detector=detector)
        self.updateSharedAttrs()
        return failures

    def applyROIClicked(self, *_):
        """ Apply button. The user is watching this one, so a camera that
        refuses the ROI has to say so -- otherwise the frame fields just snap
        back to the previous geometry with no explanation. """

        failures = self.adjustFrame()
        if not failures:
            return

        guitools.showWarning(
            self._widget,
            'ROI not applied',
            'The camera did not accept the requested ROI:\n\n'
            + '\n'.join(f'• {name}: {error}' for name, error in failures)
            + '\n\nThe settings now show the ROI the camera is actually using.'
        )

    def ROIchanged(self):
        """ Update parameters according to ROI. """
        frameStart = self._master.detectorsManager.execOnCurrent(lambda c: c.frameStart)
        ROI = self._widget.getROIGraphicsItem()
        # ROI.position/.size are in DATA-PIXEL units: the ROI visual renders
        # aligned to the (pixel-size-scaled) image via setPixelScale, but its
        # own position/size/bounds stay in pixels -- the same contract every
        # other ROI consumer uses directly (BeadRec, AlignXY, AlignAverage).
        # No pixel-size division here anymore; dividing again would scale by
        # 1/pixelSize a second time.
        pos = [round(value) for value in ROI.position]
        size = [round(value) for value in ROI.size]

        currentParams = self.getCurrentParams()
        currentParams.x0.setValue(frameStart[0] + int(pos[0]))
        currentParams.y0.setValue(frameStart[1] + int(pos[1]))
        currentParams.width.setValue(size[0])  # [0] is Width
        currentParams.height.setValue(size[1])  # [1] is Height

    def updateFrameActionButtons(self, *, detector=None):
        """ Shows the frame-related buttons appropriate for the current frame
        mode, and hides the others. """

        if detector is None:
            self.getDetectorManagerFrameExecFunc()(
                lambda c: self.updateFrameActionButtons(detector=c)
            )
            return

        params = self.allParams[detector.name]

        params.applyROI.hide()
        params.newROI.hide()
        params.abortROI.hide()
        params.saveMode.hide()
        params.deleteMode.hide()

        if params.frameMode.value() == 'Custom':
            params.applyROI.show()
            params.newROI.show()
            params.abortROI.show()
            params.saveMode.show()
        elif params.frameMode.value() != 'Full chip':
            params.deleteMode.show()

    def abortROI(self):
        """ Cancel and reset parameters of the ROI. """
        self.toggleROI(False)
        frameStart = self._master.detectorsManager.execOnCurrent(lambda c: c.frameStart)
        shapes = self._master.detectorsManager.execOnCurrent(lambda c: c.shape)

        currentParams = self.getCurrentParams()
        currentParams.x0.setValue(frameStart[0])
        currentParams.y0.setValue(frameStart[1])
        currentParams.width.setValue(shapes[0])
        currentParams.height.setValue(shapes[1])

    def saveMode(self):
        """ Save the current frame mode parameters to the mode list. """

        currentParams = self.getCurrentParams()
        x0, y0, width, height = (currentParams.x0.value(), currentParams.y0.value(),
                                 currentParams.width.value(), currentParams.height.value())

        name = guitools.askForTextInput(
            self._widget,
            'Add frame mode',
            f'Enter a name for this mode:\n(X0: {x0}; Y0: {y0}; Width: {width}; Height: {height})')

        if not name:  # No name provided
            return

        add = True
        alreadyExists = False
        if name in self._setupInfo.rois:
            alreadyExists = True
            add = guitools.askYesNoQuestion(
                self._widget,
                'Frame mode already exists',
                f'A frame mode with the name "{name}" already exists. Do you want to overwrite it"?'
            )

        if add:
            # Add in GUI
            if not alreadyExists:
                for params in self.allParams.values():
                    newModeItems = params.frameMode.opts['limits'].copy()
                    newModeItems.insert(len(newModeItems) - 1, name)
                    params.frameMode.setLimits(newModeItems)

            # Set in setup info
            self._setupInfo.setROI(name, x0, y0, width, height)
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)

            # Update selected ROI in GUI
            for params in self.allParams.values():
                params.frameMode.setValue(name)

    def deleteMode(self):
        """ Delete the current frame mode from the mode list (if it's a saved
        custom ROI). """

        currentParams = self.getCurrentParams()
        modeToDelete = currentParams.frameMode.value()

        confirmationResult = guitools.askYesNoQuestion(
            self._widget,
            'Delete frame mode?',
            f'Are you sure you want to delete the mode "{modeToDelete}"?'
        )

        if confirmationResult:
            # Remove in GUI
            for params in self.allParams.values():
                newModeItems = params.frameMode.opts['limits'].copy()
                newModeItems = [value for value in newModeItems if value != modeToDelete]
                params.frameMode.setLimits(newModeItems)

            # Remove from setup info
            self._setupInfo.removeROI(modeToDelete)
            configfiletools.saveSetupInfo(configfiletools.loadOptions()[0], self._setupInfo)

    def updateBinning(self):
        """ Update a new binning to the detector. """
        if self._suppressWriteback:
            return
        self.getDetectorManagerFrameExecFunc()(
            lambda c: c.setBinning(int(self.allParams[c.name].binning.value()))
        )
        self.updateSharedAttrs()

    @contextlib.contextmanager
    def _writebackSuppressed(self):
        """ Write the widget without letting it write back to hardware.

        A pyqtgraph param emits "user intent" regardless of who set it, so a
        plain readback re-enters setDetectorParameter/updateBinning/updateFrame
        and pushes values back at the detector -- often the *displayed* one
        rather than the one being read back.

        The obvious defence, ``param.setValue(v, blockSignal=True)``, is wrong:
        in pyqtgraph >= 0.14 ``blockSignal`` suppresses ``sigValueChanged``, and
        that signal is also what tells the tree item to repaint. Blocking it
        leaves the parameter holding the new value while the spinbox on screen
        still shows the old one -- the widget then misreports the hardware, and
        the next edit pushes the stale number it is displaying back into the
        detector. Suppress our own handlers instead and let the view update. """

        previous = self._suppressWriteback
        self._suppressWriteback = True
        try:
            yield
        finally:
            self._suppressWriteback = previous

    def updateParamsFromDetector(self, *, detector, blockSignals=False):
        """ Update the parameter values from the detector.

        This is a pure readback: the widget is a view of the detector's actual
        state. Pass ``blockSignals=True`` when the caller has already configured
        the detector itself (state restore), so the readback does not travel
        back to hardware -- see _writebackSuppressed(). """

        if blockSignals:
            with self._writebackSuppressed():
                self._updateParamsFromDetector(detector)
        else:
            self._updateParamsFromDetector(detector)

    def _updateParamsFromDetector(self, detector):
        params = self.allParams[detector.name]

        def setValue(param, value):
            param.setValue(value)

        # Detector parameters
        for parameterName, parameter in detector.parameters.items():
            paramInWidget = self._widget.trees[detector.name].p.param(parameter.group).param(
                parameterName
            )
            setValue(paramInWidget, parameter.value)

        # Frame
        setValue(params.binning, detector.binning)
        frameStart = detector.frameStart
        shape = detector.shape
        fullShape = detector.fullShape
        setValue(params.x0, frameStart[0])
        setValue(params.y0, frameStart[1])
        setValue(params.width, shape[0])
        params.width.setLimits((1, fullShape[0]))
        setValue(params.height, shape[1])
        params.height.setLimits((1, fullShape[1]))

        # Model
        setValue(params.model, detector.model)

    def _applyFrameModeFieldState(self, detector):
        """ Make the ROI fields editable only in 'Custom' mode, for the given
        detector's own frame mode. Split out of updateFrame so programmatic
        callers can get the field state right without starting an interactive
        ROI session. """

        params = self.allParams[detector.name]
        customFrame = params.frameMode.value() == 'Custom'

        for field in (params.x0, params.y0, params.width, params.height):
            field.setWritable(customFrame)
            # Call .show() to prevent view alignment issues
            field.show()

    def updateFrame(self, *, detector=None):
        """ Change the image frame size and position in the sensor. """

        if self._suppressWriteback:
            # A readback wrote the frame mode; the caller applies the field
            # state and button visibility itself. Starting an interactive ROI
            # session here would overwrite the geometry just read back.
            return

        if detector is None:
            self.getDetectorManagerFrameExecFunc()(lambda c: self.updateFrame(detector=c))
            return

        params = self.allParams[detector.name]
        # The frame mode of the detector being operated on -- NOT the displayed
        # one. Reading getCurrentParams() here made every programmatic caller
        # (__init__'s execOnAll, setDetectorROI, state restore) apply the
        # current detector's mode to whichever detector was passed in.
        frameMode = params.frameMode.value()
        customFrame = frameMode == 'Custom'

        self._applyFrameModeFieldState(detector)

        if customFrame:
            # The ROI overlay lives on the displayed image and ROIchanged()
            # writes through getCurrentParams(), so starting an interactive ROI
            # session for a detector that isn't displayed would overwrite the
            # *current* detector's frame fields. Leave the existing values be.
            if detector.name == self._master.detectorsManager.getCurrentDetectorName():
                ROIsize = (64, 64)
                ROIcenter = self._commChannel.getCenterViewbox()

                ROIpos = (ROIcenter[0] - 0.5 * ROIsize[0],
                          ROIcenter[1] - 0.5 * ROIsize[1])

                self.toggleROI(True, ROIpos, ROIsize)
                self.ROIchanged()

        else:
            if frameMode == 'Full chip':
                fullChipShape = detector.fullShape
                params.x0.setValue(0)
                params.y0.setValue(0)
                params.width.setValue(fullChipShape[0])
                params.height.setValue(fullChipShape[1])
            else:
                roiInfo = self._setupInfo.rois[frameMode]
                params.x0.setValue(roiInfo.x)
                params.y0.setValue(roiInfo.y)
                params.width.setValue(roiInfo.w)
                params.height.setValue(roiInfo.h)

            self.adjustFrame(detector=detector)

        self.syncFrameParams(doAdjustFrame=False)

    def detectorSwitched(self, newDetectorName, _=None):
        """ Called when the user switches to another detector. """
        self._widget.setDisplayedDetector(newDetectorName)
        self._widget.setImageFrameVisible(self._master.detectorsManager[newDetectorName].croppable)
        newDetectorShape = self._master.detectorsManager[newDetectorName].shape
        self._commChannel.sigAdjustFrame.emit(newDetectorShape)

    def detectorSwitchClicked(self, detectorName):
        """ Changes the current detector to the selected detector. """
        self._master.detectorsManager.setCurrentDetector(detectorName)

    def detectorNextClicked(self):
        """ Changes the current detector to the next detector. """
        self._widget.selectNextDetector()

    def syncFrameParams(self, doAdjustFrame=True, doUpdateFrameActionButtons=True):
        if self._suppressWriteback:
            # Readback of one detector's frame; copying it across every
            # detector is the opposite of reading hardware back.
            return
        currentParams = self.getCurrentParams()
        shouldSync = currentParams.allDetectorsFrame.value()

        for params in self.allParams.values():
            params.allDetectorsFrame.setValue(shouldSync)
            if shouldSync:
                params.frameMode.setValue(currentParams.frameMode.value())
                params.x0.setValue(currentParams.x0.value())
                params.y0.setValue(currentParams.y0.value())
                params.width.setValue(currentParams.width.value())
                params.height.setValue(currentParams.height.value())

        if shouldSync and doAdjustFrame:
            self.adjustFrame()

        if doUpdateFrameActionButtons:
            self.updateFrameActionButtons()

    def getCurrentParams(self):
        return self.allParams[self._master.detectorsManager.getCurrentDetectorName()]

    def getDetectorManagerFrameExecFunc(self):
        """ Returns the detector manager exec function that should be used for
        frame-related changes. """
        currentParams = self.getCurrentParams()
        detectorsManager = self._master.detectorsManager
        return (detectorsManager.execOnAll if currentParams.allDetectorsFrame.value()
                else detectorsManager.execOnCurrent)

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) < 3 or key[0] != _attrCategory:
            return

        detectorName = key[1]
        if len(key) == 3:
            if key[2] == _binningAttr:
                self.setDetectorBinning(detectorName, value)
            elif key[2] == _ROIAttr:
                self.setDetectorROI(detectorName, (value[0], value[1]), (value[2], value[3]))
        if len(key) == 4:
            if key[2] == _detectorParameterSubCategory:
                self.setDetectorParameter(detectorName, key[3], value)

    def setSharedAttr(self, detectorName, attr, value, *, isDetectorParameter=False):
        self.settingAttr = True
        try:
            if not isDetectorParameter:
                key = (_attrCategory, detectorName, attr)
            else:
                key = (_attrCategory, detectorName, _detectorParameterSubCategory, attr)
            self._commChannel.sharedAttrs[key] = value
        finally:
            self.settingAttr = False

    def updateSharedAttrs(self):
        for dName, dManager in self._master.detectorsManager:
            self.setSharedAttr(dName, _modelAttr, dManager.model)
            self.setSharedAttr(dName, _pixelSizeAttr, dManager.pixelSizeUm)
            self.setSharedAttr(dName, _binningAttr, dManager.binning)
            self.setSharedAttr(dName, _ROIAttr, [*dManager.frameStart, *dManager.shape])

            for parameterName, parameter in dManager.parameters.items():
                self.setSharedAttr(dName, parameterName, parameter.value, isDetectorParameter=True)

    @APIExport()
    def getDetectorNames(self) -> List[str]:
        """ Returns the device names of all detectors. These device names can
        be passed to other detector-related functions. """
        return self._master.detectorsManager.getAllDeviceNames()

    @APIExport(runOnUIThread=True)
    def setDetectorBinning(self, detectorName: str, binning: int) -> None:
        """ Sets binning value for the specified detector. """
        self.allParams[detectorName].binning.setValue(binning)
        self._master.detectorsManager[detectorName].setBinning(binning)

    @APIExport(runOnUIThread=True)
    def setDetectorROI(self, detectorName: str, frameStart: Tuple[int, int],
                       shape: Tuple[int, int]) -> None:
        """ Sets the ROI for the specified detector. frameStart is a tuple
        (x0, y0) and shape is a tuple (width, height). """

        detector = self._master.detectorsManager[detectorName]
        params = self.allParams[detectorName]

        # Set the mode without going through updateFrame: its 'Custom' branch
        # opens an interactive 64x64 ROI overlay whose geometry is written
        # straight back over the caller-supplied frame -- and, for a detector
        # that isn't the displayed one, over the *current* detector's fields.
        with self._writebackSuppressed():
            params.frameMode.setValue('Custom')
        self._applyFrameModeFieldState(detector)
        self.updateFrameActionButtons(detector=detector)

        params.x0.setValue(frameStart[0])
        params.y0.setValue(frameStart[1])
        params.width.setValue(shape[0])
        params.height.setValue(shape[1])
        self.adjustFrame(detector=detector)

    @APIExport(runOnUIThread=True)
    def setDetectorParameter(self, detectorName: str, parameterName: str, value: Any) -> None:
        """ Sets the specified detector-specific parameter to the specified
        value. """

        if self._suppressWriteback:
            return

        if (parameterName in ['Trigger source'] and
                self.getCurrentParams().allDetectorsFrame.value()):
            # Special case for certain parameters that will follow the "update all detectors" option
            execFunc = self._master.detectorsManager.execOnAll
        else:
            def execFunc(f): self._master.detectorsManager.execOn(detectorName, f)

        execFunc(
            lambda c: (c.setParameter(parameterName, value) and
                       self.updateParamsFromDetector(detector=c))
        )
        self.updateSharedAttrs()
    
    def refreshAdvancedProperties(self, detectorName):
        """
        Refresh advanced properties for a detector.
        
        Queries the detector manager for advanced property information and
        updates the advanced properties widget display.
        
        Args:
            detectorName: Name of the detector to refresh
        """
        try:
            # Get the detector manager
            detectorManager = self._master.detectorsManager[detectorName]
            
            # Get the advanced widget
            advancedWidget = self._widget.getAdvancedWidget(detectorName)
            if not advancedWidget:
                return
            
            # Check if manager has the method (should be true if we got here, but be safe)
            if not hasattr(detectorManager, 'getAdvancedPropertyInfo'):
                advancedWidget.showMessage(
                    'Advanced properties not supported for this detector.'
                )
                return
            
            # Get properties from manager
            properties = detectorManager.getAdvancedPropertyInfo()
            
            # Display properties in widget
            if properties:
                advancedWidget.setProperties(properties)
                self._logger.info(
                    f'Refreshed {len(properties)} advanced properties for {detectorName}'
                )
            else:
                advancedWidget.showMessage(
                    'No advanced properties available or failed to query properties.'
                )
                self._logger.warning(
                    f'No advanced properties returned for {detectorName}'
                )
        
        except Exception as e:
            self._logger.error(
                f'Failed to refresh advanced properties for {detectorName}: {e}'
            )
            advancedWidget = self._widget.getAdvancedWidget(detectorName)
            if advancedWidget:
                advancedWidget.showMessage(
                    f'Error querying properties: {str(e)}'
                )
    
    def applyAdvancedProperty(self, detectorName, propertyName, value):
        """
        Apply a change to an advanced camera property.
        
        This method is called when the user clicks Apply on a property in the
        Advanced Properties tab. It calls the detector manager's setAdvancedProperty
        method, logs the result, and refreshes the displayed value.
        
        Args:
            detectorName: Name of the detector
            propertyName: Name of the property to change
            value: New value for the property
        """
        try:
            # Get the detector manager
            detectorManager = self._master.detectorsManager[detectorName]
            
            # Check if manager has the setAdvancedProperty method
            if not hasattr(detectorManager, 'setAdvancedProperty'):
                self._logger.error(
                    f'Detector {detectorName} does not support setting advanced properties'
                )
                return
            
            # Log the change attempt
            self._logger.info(
                f'User requesting to change {propertyName} to {value} for {detectorName}'
            )
            
            # Call the manager's setAdvancedProperty method
            result = detectorManager.setAdvancedProperty(propertyName, value)
            
            # Handle the result
            if result.get('success'):
                actual_value = result.get('value')
                self._logger.info(
                    f'Successfully set {propertyName} to {actual_value} for {detectorName}'
                )
                
                # Refresh properties to show the updated value
                self.refreshAdvancedProperties(detectorName)
                
                # TODO: Could add a status message in the UI here if desired
                
            else:
                error_msg = result.get('error', 'Unknown error')
                self._logger.error(
                    f'Failed to set {propertyName} to {value} for {detectorName}: {error_msg}'
                )
                
                # TODO: Could show error dialog or status message in UI
        
        except Exception as e:
            self._logger.error(
                f'Exception while setting {propertyName} to {value} for {detectorName}: {e}'
            )
            import traceback
            traceback.print_exc()

    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current detector settings for both startup and setup modes.

        Returns detector ROI/binning/frame-mode/parameters for forAcquisition detectors only.
        Does NOT include acquisition state (running/stopped) for safety.

        Payload shape (canonical):
        {
            'detectors': {
                detectorName: {
                    'binning': int,
                    'frame_mode': str,
                    'x0': int,
                    'y0': int,
                    'width': int,
                    'height': int,
                    'parameters': {paramName: value}
                }
            }
        }

        ROI, binning and parameters are read from the DETECTOR, not from the
        widget: the widget is a view of hardware, and applyComponentState feeds
        these values straight back to ``crop``/``setBinning``/``setParameter``.
        Snapshotting the widget instead would persist ROI edits the user typed
        but never applied, and would mix widget and sensor coordinate
        conventions across the save/restore round trip. ``frame_mode`` has no
        hardware counterpart and necessarily comes from the widget.

        Read-only parameters (e.g. 'Real exposure time', 'Readout time') are
        camera-reported readings, not settings, so they are not persisted --
        they are refreshed from hardware on restore.

        Parameters the setup file owns (``detector.configOwnedParameters``, i.e.
        the camera pixel size when ``cameraPixelSizeUm`` is configured) are not
        persisted either. They are optical calibration declared in the config,
        so the config must win on every boot; snapshotting them makes editing
        the setup file a no-op until the state file is deleted by hand.

        This shape differs from the legacy SetupModesController summarizer
        (which expected roiMode/roi tuple); describeComponentState adapts to this shape.

        Returns:
            JSON-serializable dict per schema above
        """
        state = {'detectors': {}}
        
        try:
            for detectorName in self._master.detectorsManager.getAllDeviceNames():
                detector = self._master.detectorsManager[detectorName]
                if not detector.forAcquisition:
                    continue
                
                try:
                    params = self.allParams.get(detectorName)
                    if not params:
                        continue

                    frameStart = detector.frameStart
                    shape = detector.shape
                    detector_state = {
                        'binning': detector.binning,
                        'frame_mode': params.frameMode.value() if hasattr(params.frameMode, 'value') else None,
                        'x0': frameStart[0],
                        'y0': frameStart[1],
                        'width': shape[0],
                        'height': shape[1],
                        'parameters': {}
                    }

                    # Get editable detector-specific parameters (exposure, gain, etc.)
                    configOwned = getattr(detector, 'configOwnedParameters', frozenset())
                    if hasattr(detector, 'parameters'):
                        for paramName, parameter in detector.parameters.items():
                            if not getattr(parameter, 'editable', True):
                                continue
                            if paramName in configOwned:
                                continue
                            try:
                                detector_state['parameters'][paramName] = parameter.value
                            except Exception as e:
                                self._logger.debug(
                                    f'Could not save parameter {paramName} for {detectorName}: {e}'
                                )

                    state['detectors'][detectorName] = detector_state

                except Exception as e:
                    self._logger.warning(
                        f'Failed to save state for detector {detectorName}: {e}'
                    )
        
        except Exception as e:
            self._logger.error(f'Failed to save detector settings state: {e}')
        
        return state
    
    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore detector settings from a snapshot.
        
        BEHAVIOR (IDENTICAL for both STARTUP_RESTORE and SETUP_MODE_APPLY):
        - Restore ROI (x0, y0, width, height), binning, frame mode
        - Restore editable detector-specific parameters (exposure, trigger, etc.)
        - NEVER start acquisition or live view in either mode

        Per spec §2.3, detector ROI/binning/trigger mode are allowed in both modes.
        Acquisition control is not part of detector settings state.

        The detector is configured DIRECTLY and the widget is then refreshed from
        the resulting hardware state with signals blocked. Driving the widget
        params and relying on their Qt signals to reach hardware does not work:
        those handlers resolve their target through getCurrentParams() /
        getDetectorManagerFrameExecFunc(), so they act on the *displayed*
        detector rather than the one being restored -- silently dropping the ROI
        entirely, applying one detector's binning to another, and (in 'Custom'
        mode) overwriting an already-restored detector's frame with a fresh
        64x64 overlay.

        ROI values are sensor-pixel values straight from a previous
        ``detector.crop``/``frameStart``/``shape``, so they are passed to
        ``crop`` unconverted -- adjustFrame()'s binning multiply and modulus
        rounding sanitize *human* input and must not be applied a second time to
        values that already came back from hardware.

        Args:
            state: Dict returned by getComponentState()
            applyMode: ComponentStateApplyMode.STARTUP_RESTORE or SETUP_MODE_APPLY

        Returns:
            List of RestoreWarning strings (empty if fully successful). Ones
            that mean hardware did not take a setting are marked critical;
            entries the saved state simply no longer applies to are not, so a
            state file that predates a setup change does not raise an alarm
            the operator learns to click away.
        """
        warnings = []
        restoredDetectors = []

        def skipped(message):
            """The saved state does not apply here; no hardware was left wrong."""
            return RestoreWarning(message, critical=False)

        try:
            detectors_state = state.get('detectors', {})
            known_detectors = set(self._master.detectorsManager.getAllDeviceNames())

            for detectorName, detector_state in detectors_state.items():
                if detectorName not in known_detectors:
                    warnings.append(skipped(
                        f'Detector "{detectorName}" not present in current setup; skipped.'))
                    continue

                detector = self._master.detectorsManager[detectorName]
                if not detector.forAcquisition:
                    continue

                params = self.allParams.get(detectorName)
                if not params:
                    warnings.append(skipped(
                        f'Detector "{detectorName}" has no widget params; skipped.'))
                    continue

                try:
                    # 1. Binning, on this detector (crop sizes are relative to it)
                    binning = detector_state.get('binning')
                    if binning is not None:
                        try:
                            detector.setBinning(int(binning))
                        except Exception as e:
                            warnings.append(f'Could not restore binning for {detectorName}: {e}')

                    # 2. ROI, on this detector
                    roi = tuple(detector_state.get(key) for key in ('x0', 'y0', 'width', 'height'))
                    if all(value is not None for value in roi):
                        if detector.croppable:
                            try:
                                detector.crop(*(int(value) for value in roi))
                            except Exception as e:
                                warnings.append(f'Could not restore ROI for {detectorName}: {e}')
                        # Non-croppable detectors (APD/PMT/TimeTagger) derive their
                        # shape from the scan; there is no ROI to restore.

                    # 3. Editable parameters only. Read-only ones ('Real exposure
                    # time', 'Readout time', ...) are camera readings, not
                    # settings; writing a saved value would overwrite what the
                    # hardware just reported with a stale number. Legacy state
                    # files still contain them -- ignore those entries. Same for
                    # setup-file-owned parameters (the camera pixel size).
                    parameters_state = detector_state.get('parameters', {})
                    configOwned = getattr(detector, 'configOwnedParameters', frozenset())
                    for paramName, value in parameters_state.items():
                        parameter = getattr(detector, 'parameters', {}).get(paramName)
                        if parameter is None:
                            warnings.append(skipped(
                                f'Parameter "{paramName}" not present on {detectorName}; skipped.'
                            ))
                            continue
                        if not getattr(parameter, 'editable', True):
                            continue
                        if paramName in configOwned:
                            # The setup file owns this one. Snapshots written by
                            # an older build still carry it, and restoring that
                            # value is exactly the bug this guard exists for --
                            # it silently reinstates the previous calibration
                            # and every recording gets the wrong pixel size.
                            if value != parameter.value:
                                self._logger.info(
                                    f'Ignoring saved {paramName}={value} for'
                                    f' {detectorName}: the setup file sets'
                                    f' {parameter.value}, which takes precedence.'
                                )
                            continue
                        try:
                            detector.setParameter(paramName, value)
                        except Exception as e:
                            warnings.append(
                                f'Could not restore parameter {paramName} for {detectorName}: {e}'
                            )

                    # 4. The widget is a view of what the hardware actually took.
                    frameMode = detector_state.get('frame_mode')
                    if frameMode is not None:
                        try:
                            with self._writebackSuppressed():
                                params.frameMode.setValue(frameMode)
                        except Exception as e:
                            warnings.append(f'Could not restore frame mode for {detectorName}: {e}')
                    # The hardware is already configured at this point, so a
                    # failure here does not undo the restore -- it leaves the
                    # widget showing pre-restore values while the detector runs
                    # with the restored ones. That silent divergence is worse
                    # than the original failure: the GUI then misreports what
                    # every recording is actually calibrated with. Report it and
                    # keep going rather than aborting the whole detector.
                    try:
                        self.updateParamsFromDetector(detector=detector, blockSignals=True)
                        self._applyFrameModeFieldState(detector)
                        self.updateFrameActionButtons(detector=detector)
                    except Exception as e:
                        warnings.append(
                            f'Restored {detectorName} but could not refresh its settings'
                            f' display; the shown values may not match the detector: {e}'
                        )

                    restoredDetectors.append(detectorName)

                except Exception as e:
                    warnings.append(f'Failed to restore state for detector {detectorName}: {e}')

            # 5. One shared-attribute sync once every detector is settled.
            if restoredDetectors:
                self.updateSharedAttrs()

                # The viewer still shows the pre-restore frame geometry.
                currentDetectorName = self._master.detectorsManager.getCurrentDetectorName()
                if currentDetectorName in restoredDetectors:
                    self._widget.hideROI()
                    self._commChannel.sigAdjustFrame.emit(
                        self._master.detectorsManager[currentDetectorName].shape
                    )

        except Exception as e:
            warnings.append(f'Failed to restore detector settings state: {e}')

        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved detector settings.
        
        Lifted from SetupModesController._summarizeSavedDetectorState,
        adapted to the canonical payload shape (x0/y0/width/height instead of roi tuple).
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        detectors = (state or {}).get("detectors") or {}
        if not detectors:
            return ["  no detector state"]
        
        summaries = []
        for detectorName, detectorState in sorted(detectors.items(), key=lambda item: str(item[0])):
            frame_mode = detectorState.get("frame_mode")
            summaries.append(f"  {detectorName}:")
            if frame_mode is not None:
                summaries.append(f"    mode: {self._fmt(frame_mode)}")
            
            # Reconstruct ROI from x0, y0, width, height
            x0 = detectorState.get("x0")
            y0 = detectorState.get("y0")
            width = detectorState.get("width")
            height = detectorState.get("height")
            if all(v is not None for v in [x0, y0, width, height]):
                roi = [x0, y0, width, height]
                summaries.append(f"    ROI: {self._fmt(roi)}")
            
            if detectorState.get("binning") is not None:
                summaries.append(f"    binning: {self._fmt(detectorState.get('binning'))}")
            
            parameters = detectorState.get("parameters") or {}
            triggerText = self._findSavedTriggerText(parameters)
            if triggerText:
                summaries.append(f"    {triggerText}")
        
        return summaries
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved detector settings.
        
        Detector ROI/binning/parameters carry no activation hazard; they are
        passive configuration. Returns empty list for both apply modes.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: The mode in which the state would be applied
            context: Optional consumer-provided context (unused here)
        
        Returns:
            Empty list (no hazards)
        """
        return []
    
    # Helper methods (lifted from SetupModesController)
    
    def _findSavedTriggerText(self, parameters):
        """Extract trigger parameter text from detector parameters dict."""
        for parameterName, parameterState in parameters.items():
            if "trigger" not in parameterName.lower():
                continue
            value = parameterState.get("value") if isinstance(parameterState, dict) else parameterState
            return f"{parameterName}: {self._fmt(value)}"
        return None
    
    def _fmt(self, value):
        """Format a value for human-readable display."""
        if value is None:
            return "None"
        if isinstance(value, bool):
            return self._onOff(value)
        if isinstance(value, float):
            return f"{value:.4g}"
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(self._fmt(item) for item in value) + "]"
        return str(value)
    
    def _onOff(self, value):
        """Convert boolean to ON/OFF string."""
        return "ON" if bool(value) else "OFF"


_attrCategory = 'Detector'
_modelAttr = 'Model'
_pixelSizeAttr = 'Pixel size'
_binningAttr = 'Binning'
_ROIAttr = 'ROI'
_detectorParameterSubCategory = 'Param'


# Copyright (C) 2020-2021 ImSwitch developers
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
