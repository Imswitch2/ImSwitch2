import json
import os

import numpy as np

from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.managers.SLMManager import MaskMode, Direction
from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


class SLMController(StatefulComponentMixin, ImConWidgetController):
    """Linked to SLMWidget."""

    # StatefulComponentMixin attributes
    componentName = 'SLM'
    setupModeDisplayName = 'SLM'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'spatial_light_modulator'
    setupModeApplyPriority = SetupModeApplyPriority.SPATIAL_LIGHT_MODULATOR
    setupModeHardwareCritical = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)

        self.slmDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_slm')
        if not os.path.exists(self.slmDir):
            os.makedirs(self.slmDir)

        if self._setupInfo.slm is None:
            self._widget.replaceWithError('SLM is not configured in your setup file.')
            return

        self._widget.initSLMDisplay(self._setupInfo.slm.monitorIdx)

        # Connect CommunicationChannel signals
        self._commChannel.sigSLMMaskUpdated.connect(lambda mask: self.displayMask(mask))

        # Connect SLMWidget signals
        self._widget.controlPanel.upButton.clicked.connect(
            lambda: self.moveMask(Direction.Up))  # change 'up' to (x,y)=(0,1)
        self._widget.controlPanel.downButton.clicked.connect(
            lambda: self.moveMask(Direction.Down))  # change 'down' to (x,y)=(0,-1)
        self._widget.controlPanel.leftButton.clicked.connect(
            lambda: self.moveMask(Direction.Left))  # change 'left' to (x,y)=(-1,0)
        self._widget.controlPanel.rightButton.clicked.connect(
            lambda: self.moveMask(Direction.Right))  # change 'right' to (x,y)=(1,0)

        self._widget.controlPanel.saveButton.clicked.connect(self.saveParams)
        self._widget.controlPanel.loadButton.clicked.connect(self.loadParams)

        self._widget.controlPanel.donutButton.clicked.connect(lambda: self.setMask(MaskMode.Donut))
        self._widget.controlPanel.tophatButton.clicked.connect(
            lambda: self.setMask(MaskMode.Tophat))

        self._widget.controlPanel.blackButton.clicked.connect(lambda: self.setMask(MaskMode.Black))
        self._widget.controlPanel.gaussianButton.clicked.connect(
            lambda: self.setMask(MaskMode.Gauss))

        self._widget.controlPanel.halfButton.clicked.connect(lambda: self.setMask(MaskMode.Half))
        self._widget.controlPanel.quadrantButton.clicked.connect(
            lambda: self.setMask(MaskMode.Quad))
        self._widget.controlPanel.hexButton.clicked.connect(lambda: self.setMask(MaskMode.Hex))
        self._widget.controlPanel.splitbullButton.clicked.connect(
            lambda: self.setMask(MaskMode.Split))

        self._widget.applyChangesButton.clicked.connect(self.applyParams)
        self._widget.sigSLMDisplayToggled.connect(self.toggleSLMDisplay)
        self._widget.sigSLMMonitorChanged.connect(self.monitorChanged)

        # Initial SLM display
        self.displayMask(self._master.slmManager.maskCombined)

        # Register for unified state persistence (canonical name)
        # Only register when SLM is configured (not on error path above)
        getWidgetStatePersistence().register('SLM', self)

    @APIExport(runOnUIThread=True)
    def toggleSLMDisplay(self, enabled):
        self._widget.setSLMDisplayVisible(enabled)

    def monitorChanged(self, monitor):
        self._widget.setSLMDisplayMonitor(monitor)

    def displayMask(self, maskCombined):
        """ Display the mask in the SLM display. Originates from slmPy:
        https://github.com/wavefrontshaping/slmPy """

        arr = maskCombined.image()

        # Padding: Like they do in the software
        pad = np.zeros((600, 8), dtype=np.uint8)
        arr = np.append(arr, pad, 1)

        # Create final image array
        h, w = arr.shape[0], arr.shape[1]

        if len(arr.shape) == 2:
            # Array is grayscale
            arrGray = arr.copy()
            arrGray.shape = h, w, 1
            img = np.concatenate((arrGray, arrGray, arrGray), axis=2)
        else:
            img = arr

        self._widget.updateSLMDisplay(img)

    # Button pressed functions
    def moveMask(self, direction):
        amount = self._widget.controlPanel.incrementSpinBox.value()
        mask = self._widget.controlPanel.maskComboBox.currentIndex()
        self._master.slmManager.moveMask(mask, direction, amount)
        image = self._master.slmManager.update(maskChange=True, aberChange=True, tiltChange=True)
        self.updateDisplayImage(image)

    def saveParams(self):
        obj = self._widget.controlPanel.objlensComboBox.currentText()
        if obj == 'No objective':
            self.__logger.error('You have to choose an objective from the drop down menu.')
            return
        elif obj == 'Oil':
            filename = 'info_oil.json'
        elif obj == 'Glycerol':
            filename = 'info_glyc.json'
        else:
            raise ValueError(f'Unsupported objective "{obj}"')

        slm_info_dict = self.getInfoDict(self._widget.slmParameterTree.p,
                                         self._widget.aberParameterTree.p,
                                         self._master.slmManager.getCenters())
        with open(os.path.join(self.slmDir, filename), 'w') as f:
            json.dump(slm_info_dict, f, indent=4)
        self.__logger.info(f'Saved SLM parameters for {obj} objective.')

    def getInfoDict(self, generalParams=None, aberParams=None, centers=None):
        state_general = None
        state_pos = None
        state_aber = None

        if generalParams is not None:
            # create dict for general params
            generalparamnames = ["radius", "sigma", "rotationAngle", "tiltAngle"]
            state_general = {generalparamname: float(
                generalParams.param("general").param(generalparamname).value()) for generalparamname
                             in generalparamnames}

        if aberParams is not None:
            # create dict for aberration params
            masknames = ["left", "right"]
            aberparamnames = ["tilt", "tip", "defocus", "spherical", "verticalComa",
                              "horizontalComa", "verticalAstigmatism", "obliqueAstigmatism"]
            state_aber = dict.fromkeys(masknames)
            for maskname in masknames:
                state_aber[maskname] = {
                    aberparamname: float(aberParams.param(maskname).param(aberparamname).value())
                    for aberparamname in aberparamnames}

        if centers is not None:
            # create dict for position params
            state_pos = dict.fromkeys(masknames)
            for maskname in masknames:
                state_pos[maskname] = {
                    "xcenter": int(centers[maskname][0]),
                    "ycenter": int(centers[maskname][1])
                }

        info_dict = {
            "general": state_general,
            "position": state_pos,
            "aber": state_aber
        }
        return info_dict

    @APIExport(runOnUIThread=True)
    def loadParams(self):
        obj = self._widget.controlPanel.objlensComboBox.currentText()
        if obj == 'No objective':
            self.__logger.error('You have to choose an objective from the drop down menu.')
            return
        elif obj == 'Oil':
            filename = 'info_oil.json'
        elif obj == 'Glycerol':
            filename = 'info_glyc.json'
        else:
            raise ValueError(f'Unsupported objective "{obj}"')

        with open(os.path.join(self.slmDir, filename), 'rb') as f:
            slm_info_dict = json.load(f)
            state_general = slm_info_dict["general"]
            state_pos = slm_info_dict["position"]
            state_aber = slm_info_dict["aber"]

        self.setParamTree(state_general=state_general, state_aber=state_aber)
        self._master.slmManager.setGeneral(state_general)
        self._master.slmManager.setCenters(state_pos)
        self._master.slmManager.setAberrationFactors(state_aber)
        self._master.slmManager.saveState(state_general, state_pos, state_aber)
        image = self._master.slmManager.update(maskChange=True, tiltChange=True, aberChange=True)
        self.updateDisplayImage(image)

    def setParamTree(self, state_general, state_aber):
        generalParams = self._widget.slmParameterTree.p
        aberParams = self._widget.aberParameterTree.p

        generalparamnames = ["radius", "sigma", "rotationAngle", "tiltAngle"]
        for generalparamname in generalparamnames:
            generalParams.param("general").param(generalparamname).setValue(
                float(state_general[generalparamname])
            )

        masknames = ["left", "right"]
        aberparamnames = ["tilt", "tip", "defocus", "spherical", "verticalComa", "horizontalComa",
                          "verticalAstigmatism", "obliqueAstigmatism"]
        for maskname in masknames:
            for aberparamname in aberparamnames:
                aberParams.param(maskname).param(aberparamname).setValue(
                    float(state_aber[maskname][aberparamname])
                )
    @APIExport(runOnUIThread=True)
    def setMask(self, maskMode):
        mask = self._widget.controlPanel.maskComboBox.currentIndex()  # 0 = donut (left), 1 = tophat (right)
        if isinstance(maskMode, str):
            maskMode = getattr(MaskMode, maskMode)
            self.__logger.info(f'Maskmode was provided as string') #Simone debugging
        else:
            self.__logger.info(f'Maskmode was provided as attribute') #Simone debugging
        self._master.slmManager.setMask(mask, maskMode)
        slm_info_dict = self.getInfoDict(generalParams=self._widget.slmParameterTree.p,
                                        aberParams=self._widget.aberParameterTree.p)
        self.applyAberrations(slm_info_dict["aber"], mask)
        image = self._master.slmManager.update(maskChange=True, tiltChange=True, aberChange=True)
        self.updateDisplayImage(image)

    def applyParams(self):
        slm_info_dict = self.getInfoDict(generalParams=self._widget.slmParameterTree.p,
                                         aberParams=self._widget.aberParameterTree.p)
        self.applyGeneral(slm_info_dict["general"])
        self.applyAberrations(slm_info_dict["aber"])
        image = self._master.slmManager.update(maskChange=True, tiltChange=True, aberChange=True)
        self.updateDisplayImage(image)
        self._master.slmManager.saveState(state_general=slm_info_dict["general"],
                                          state_aber=slm_info_dict["aber"])

    def applyGeneral(self, info_dict):
        self._master.slmManager.setGeneral(info_dict)

    def applyAberrations(self, info_dict, mask=None):
        self._master.slmManager.setAberrations(info_dict, mask)

    def updateDisplayImage(self, image):
        image = np.fliplr(image.transpose())
        self._widget.img.setImage(image, autoLevels=True, autoDownsample=False)

    # StatefulComponentMixin implementation
    def getComponentState(self) -> dict:
        """Snapshot current SLM state for both startup and setup modes.

        Returns a JSON-serializable dict containing inline SLM parameters:
        general params (radius, sigma, rotation/tilt angles), position centers
        (left/right mask centers), and aberration coefficients (left/right).

        Returns:
            {
                'general': {
                    'radius': float,
                    'sigma': float,
                    'rotationAngle': float,
                    'tiltAngle': float
                },
                'position': {
                    'left': {'xcenter': int, 'ycenter': int},
                    'right': {'xcenter': int, 'ycenter': int}
                },
                'aber': {
                    'left': {aberparamname: float, ...},
                    'right': {aberparamname: float, ...}
                },
                'objective': str | None
            }
        """
        slm_info_dict = self.getInfoDict(
            generalParams=self._widget.slmParameterTree.p,
            aberParams=self._widget.aberParameterTree.p,
            centers=self._master.slmManager.getCenters()
        )

        # Include selected objective if available
        obj = self._widget.controlPanel.objlensComboBox.currentText()
        if obj and obj != 'No objective':
            slm_info_dict['objective'] = obj
        else:
            slm_info_dict['objective'] = None

        return slm_info_dict

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore SLM state from a snapshot.

        Restoring the phase pattern (params) is permitted in BOTH modes as it is
        a display pattern, not hazardous emission. Display visibility (turning
        the SLM display output ON/visible) is NOT part of the persisted state,
        so both modes apply params identically. Users manually control display
        visibility via the widget toggle.

        ALWAYS (both modes):
        - Set general params (radius, sigma, rotation/tilt angles)
        - Set position centers (left/right mask centers)
        - Set aberration coefficients (left/right)
        - Recompute and redisplay the phase mask

        Args:
            state: Dict returned by getComponentState()
            applyMode: ComponentStateApplyMode.STARTUP_RESTORE or SETUP_MODE_APPLY

        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []

        state_general = state.get('general')
        state_pos = state.get('position')
        state_aber = state.get('aber')

        if state_general is None and state_pos is None and state_aber is None:
            warnings.append('No SLM state to restore (all sections missing).')
            return warnings

        try:
            # Apply general params
            if state_general:
                self._master.slmManager.setGeneral(state_general)
                self.setParamTree(state_general=state_general, state_aber=state_aber or {})

            # Apply position centers
            if state_pos:
                self._master.slmManager.setCenters(state_pos)

            # Apply aberration coefficients
            if state_aber:
                self._master.slmManager.setAberrationFactors(state_aber)
                if not state_general:  # Only update param tree if not already done above
                    self.setParamTree(state_general={}, state_aber=state_aber)

            # Save state to manager and recompute mask
            self._master.slmManager.saveState(state_general, state_pos, state_aber)
            image = self._master.slmManager.update(maskChange=True, tiltChange=True, aberChange=True)
            self.updateDisplayImage(image)

        except Exception as e:
            warnings.append(f'Failed to apply SLM state: {e}')
            return warnings

        # Restore objective selection (UI only)
        obj = state.get('objective')
        if obj:
            idx = self._widget.controlPanel.objlensComboBox.findText(obj)
            if idx >= 0:
                self._widget.controlPanel.objlensComboBox.setCurrentIndex(idx)
            else:
                warnings.append(f'Objective "{obj}" not found in dropdown; skipped.')

        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved SLM state.

        Summarizes the inline params directly: general params (radius, sigma,
        angles), mask centers, and which aberration terms are non-zero.

        Args:
            state: Dict returned by getComponentState()

        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        state = state or {}
        summaries = []

        obj = state.get('objective')
        if obj:
            summaries.append(f"  objective: {obj}")

        # General params
        general = state.get('general')
        if general:
            summaries.append("  general:")
            summaries.append(f"    radius: {general.get('radius')}, sigma: {general.get('sigma')}")
            summaries.append(
                f"    rotation: {general.get('rotationAngle')}°, tilt: {general.get('tiltAngle')}°"
            )

        # Position centers
        position = state.get('position')
        if position:
            summaries.append("  mask centers:")
            for mask in ['left', 'right']:
                pos = position.get(mask)
                if pos:
                    summaries.append(f"    {mask}: ({pos.get('xcenter')}, {pos.get('ycenter')})")

        # Aberration coefficients (only non-zero)
        aber = state.get('aber')
        if aber:
            summaries.append("  aberrations:")
            for mask in ['left', 'right']:
                mask_aber = aber.get(mask)
                if mask_aber:
                    nonzero = [f"{k}={v}" for k, v in mask_aber.items() if v != 0.0]
                    if nonzero:
                        summaries.append(f"    {mask}: {', '.join(nonzero)}")
                    else:
                        summaries.append(f"    {mask}: all zero")

        return summaries or ["  no SLM state"]

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify hazards in saved SLM state.

        SLM phase masks are display patterns with no laser-power-like hazard,
        so this always returns an empty list for both apply modes.

        Args:
            state: Dict returned by getComponentState()
            applyMode: The mode in which the state would be applied
            context: Optional consumer-provided context (unused for SLM)

        Returns:
            Empty list (no hazards for SLM phase patterns)
        """
        return []


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
