from .DataFrameController import DataFrameController
from .WatcherFrameController import WatcherFrameController
from .LiveModeController import LiveModeController
from .ReconstructionViewController import ReconstructionViewController
from .GraphController import GraphController
from .ScanParamsController import ScanParamsController
from .WidefieldStarssBatchController import WidefieldStarssBatchController
from .FileIOController import FileIOController
from .ReconstructorManagerController import ReconstructorManagerController
from .MoNaLISAController import MoNaLISAController
from .basecontrollers import ImProcessWidgetController


class ImProcessMainViewController(ImProcessWidgetController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._commChannel.extension = self._widget.extension

        self.dataFrameController = self._factory.createController(
            DataFrameController, self._widget.dataFrame
        )
        self.reconstructionController = self._factory.createController(
            ReconstructionViewController, self._widget.reconstructionWidget
        )
        self.watcherFrameController = self._factory.createController(
            WatcherFrameController, self._widget.watcherFrame
        )
        self.liveModeController = self._factory.createController(
            LiveModeController, self._widget.watcherFrame, mainController=self
        )
        self.wfsBatchController = self._factory.createController(
            WidefieldStarssBatchController, self._widget, mainController=self
        )
        self.fileIOController = self._factory.createController(
            FileIOController, self._widget, mainController=self
        )
        self.graphController = None
        if self._widget.graphWidget is not None:
            self.graphController = self._factory.createController(
                GraphController, self._widget.graphWidget
            )
        self.scanParamsController = self._factory.createController(
            ScanParamsController, self._widget.scanParamsDialog
        )
        self.reconstructorManager = self._factory.createController(
            ReconstructorManagerController, self._widget, mainController=self
        )
        self.monalisaController = self._factory.createController(
            MoNaLISAController, self._widget, mainController=self
        )

        # Initialize the active reconstructor after all subsidiary controllers exist
        self._activeReconstructor = None
        self.reconstructorManager.initActiveReconstructor()

        self._currentDataObj = None

        self._commChannel.sigDataFolderChanged.connect(self.fileIOController.dataFolderChanged)
        self._commChannel.sigSaveFolderChanged.connect(self.fileIOController.saveFolderChanged)
        self._commChannel.sigCurrentDataChanged.connect(self.currentDataChanged)
        self._commChannel.sigScanParamsUpdated.connect(self.monalisaController.scanParamsUpdated)
        self._commChannel.sigReconstruct.connect(self.reconstructorManager.reconstruct)

        self._widget.sigSaveReconstruction.connect(lambda: self.fileIOController.saveCurrent('reconstruction'))
        self._widget.sigSaveReconstructionAll.connect(lambda: self.fileIOController.saveAll('reconstruction'))
        self._widget.sigSaveCoeffs.connect(lambda: self.fileIOController.saveCurrent('coefficients'))
        self._widget.sigSaveCoeffsAll.connect(lambda: self.fileIOController.saveAll('coefficients'))
        self._widget.sigSetDataFolder.connect(self.fileIOController.setDataFolder)
        self._widget.sigSetSaveFolder.connect(self.fileIOController.setSaveFolder)

        self._widget.sigReconstuctCurrent.connect(self.reconstructorManager.reconstructCurrent)
        self._widget.sigReconstructMultiConsolidated.connect(
            lambda: self.reconstructorManager.reconstructMulti(consolidate=True)
        )
        self._widget.sigReconstructMultiIndividual.connect(
            lambda: self.reconstructorManager.reconstructMulti(consolidate=False)
        )
        self._widget.sigQuickLoadData.connect(self.fileIOController.quickLoadData)
        self._widget.sigUpdate.connect(lambda: self.monalisaController.updateScanParams(applyOnCurrentRecon=True))

        self._widget.sigShowPatternChanged.connect(self.monalisaController.togglePattern)
        self._widget.sigFindPattern.connect(self.monalisaController.findPattern)
        self._widget.sigShowScanParamsClicked.connect(self.monalisaController.showScanParamsDialog)
        self._widget.sigPatternParamsChanged.connect(self.monalisaController.updatePattern)
        self._widget.sigFilesDropped.connect(self.fileIOController.handleDroppedFiles)

        # The Parameters-dock picker lets the user flip between registered
        # reconstructors on the fly. The controller is the authority on the
        # registry, so the view just signals the chosen plugin id.
        try:
            self._widget.sigActiveReconstructorChanged.connect(
                self.reconstructorManager._on_user_changed_reconstructor
            )
        except AttributeError:
            # Older view builds without the picker degrade silently.
            pass
        self.monalisaController.updatePattern()
        self.monalisaController.updateScanParams()

    def currentDataChanged(self, dataObj):
        """Thin dispatcher: set shared state and delegate to specialized
        controllers for parameter loading (reconstructor manager) and
        MoNaLISA scan-param parsing."""
        self._currentDataObj = dataObj

        # Load reconstructor parameters from dataset metadata (best-effort).
        if hasattr(self._widget.parTree, "load_from_attrs"):
            try:
                self._widget.parTree.load_from_attrs(dataObj.attrs or {})
            except Exception as exc:
                self._logger.warning(f"Could not load reconstructor params from metadata: {exc}")

        # MoNaLISA-specific scan-params housekeeping (also best-effort).
        self.monalisaController.parseScanParamsFromAttrs(dataObj)

        # Pass-through reconstructors don't require an explicit click — the
        # data is the result. Route to the viewer the moment a current
        # DataObj is set, so 'view-only' and similar plugins display files
        # in a single user action instead of three.
        if (
            self._activeReconstructor is not None
            and getattr(self._activeReconstructor, 'is_pass_through', False)
            and dataObj is not None
        ):
            try:
                self.reconstructorManager.reconstruct([dataObj], consolidate=False)
            except Exception as exc:
                self._logger.warning(
                    f"Pass-through auto-route failed for {self._activeReconstructor.id}: {exc}"
                )



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
