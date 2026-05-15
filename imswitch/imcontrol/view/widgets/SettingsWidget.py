from pyqtgraph.parametertree import ParameterTree, Parameter
from qtpy import QtCore, QtWidgets, QtGui

from imswitch.imcommon.model import shortcut
from imswitch.imcommon.view.guitools import naparitools
from imswitch.imcontrol.view import guitools
from .basewidgets import Widget


class CamParamTree(ParameterTree):
    """ Making the ParameterTree for configuration of the detector during imaging
    """

    def __init__(self, detectorParameters, detectorActions, supportedBinnings, roiInfos,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)

        BinTip = ("Sets binning mode. Binning mode specifies if and how \n"
                  "many pixels are to be read out and interpreted as a \n"
                  "single pixel value.")

        # Parameter tree for the detector configuration
        params = [{'name': 'Model', 'type': 'str', 'readonly': True},
                  {'name': 'Image frame', 'type': 'group', 'children': [
                      {'name': 'Binning', 'type': 'list', 'value': 1,
                       'values': supportedBinnings, 'tip': BinTip},
                      {'name': 'Mode', 'type': 'list', 'value': 'Full chip',
                       'values': ['Full chip'] + list(roiInfos.keys()) + ['Custom']},
                      {'name': 'X0', 'type': 'int', 'value': 0, 'limits': (0, 65535)},
                      {'name': 'Y0', 'type': 'int', 'value': 0, 'limits': (0, 65535)},
                      {'name': 'Width', 'type': 'int', 'value': 1, 'limits': (1, 65535)},
                      {'name': 'Height', 'type': 'int', 'value': 1, 'limits': (1, 65535)},
                      {'name': 'Apply', 'type': 'action', 'title': 'Apply'},
                      {'name': 'New ROI', 'type': 'action', 'title': 'New ROI'},
                      {'name': 'Abort ROI', 'type': 'action', 'title': 'Abort ROI'},
                      {'name': 'Save mode', 'type': 'action',
                       'title': 'Save current parameters as mode'},
                      {'name': 'Delete mode', 'type': 'action',
                       'title': 'Remove current mode from list'},
                      {'name': 'Update all detectors', 'type': 'bool', 'value': False}
                  ]}]

        detectorParamGroups = {}
        for detectorParameterName, detectorParameter in detectorParameters.items():
            if detectorParameter.group not in detectorParamGroups:
                # Create group
                detectorParamGroups[detectorParameter.group] = {
                    'name': detectorParameter.group, 'type': 'group', 'children': []
                }

            detectorParameterType = type(detectorParameter).__name__
            if detectorParameterType == 'DetectorNumberParameter':
                pyqtParam = {
                    'name': detectorParameterName,
                    'type': 'float',
                    'value': detectorParameter.value,
                    'readonly': not detectorParameter.editable,
                    'siPrefix': detectorParameter.valueUnits in ['s'],
                    'suffix': detectorParameter.valueUnits,
                    'decimals': 5,
                    'step': 0.001,
                }
            elif detectorParameterType == 'DetectorListParameter':
                pyqtParam = {
                    'name': detectorParameterName,
                    'type': 'list',
                    'value': detectorParameter.value,
                    'readonly': not detectorParameter.editable,
                    'values': detectorParameter.options
                }
            else:
                raise TypeError(f'Unsupported detector parameter type "{detectorParameterType}"')

            detectorParamGroups[detectorParameter.group]['children'].append(pyqtParam)

        for detectorActionName, detectorAction in detectorActions.items():
            if detectorAction.group not in detectorParamGroups:
                # Create group
                detectorParamGroups[detectorAction.group] = {
                    'name': detectorAction.group, 'type': 'group', 'children': []
                }

            detectorParamGroups[detectorAction.group]['children'].append(
                {'name': detectorActionName, 'type': 'action', 'title': detectorActionName}
            )

        params += list(detectorParamGroups.values())

        self.p = Parameter.create(name='params', type='group', children=params)
        self.setParameters(self.p, showTop=False)
        self._writable = True

    def setImageFrameVisible(self, visible):
        """ Sets whetehr the image frame settings are visible. """
        framePar = self.p.param('Image frame')
        framePar.setOpts(visible=visible)

    @property
    def writable(self):
        return self._writable

    @writable.setter
    def writable(self, value):
        """
        property to set basically the whole parameters tree as writable
        (value=True) or not writable (value=False)
        useful to set it as not writable during recording
        """
        self._writable = value
        framePar = self.p.param('Image frame')
        framePar.param('Binning').setWritable(value)
        framePar.param('Mode').setWritable(value)
        framePar.param('X0').setWritable(value)
        framePar.param('Y0').setWritable(value)
        framePar.param('Width').setWritable(value)
        framePar.param('Height').setWritable(value)

        # WARNING: If Apply and New ROI button are included here they will
        # emit status changed signal and their respective functions will be
        # called... -> problems.
        timingPar = self.p.param('Timings')
        timingPar.param('Set exposure time').setWritable(value)

    def attrs(self):
        attrs = []
        for ParName in self.p.getValues():
            Par = self.p.param(str(ParName))
            if not (Par.hasChildren()):
                attrs.append((str(ParName), Par.value()))
            else:
                for sParName in Par.getValues():
                    sPar = Par.param(str(sParName))
                    if sPar.type() != 'action':
                        if not (sPar.hasChildren()):
                            attrs.append((str(sParName), sPar.value()))
                        else:
                            for ssParName in sPar.getValues():
                                ssPar = sPar.param(str(ssParName))
                                attrs.append((str(ssParName), ssPar.value()))
        return attrs


class AdvancedPropertiesWidget(QtWidgets.QWidget):
    """
    Widget for displaying and editing advanced camera properties.
    
    Shows a table with property metadata: name, current value, edit control, and Apply button.
    For writable properties, provides appropriate edit controls:
    - QDoubleSpinBox/QSpinBox for numeric properties with ranges
    - QComboBox for MODE properties with text options
    - Read-only display for non-writable properties
    """
    
    sigRefreshClicked = QtCore.Signal()
    sigPropertyChangeRequested = QtCore.Signal(str, object)  # (propertyName, newValue)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Store current properties data
        self._properties = []
        
        # Create layout
        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        
        # Info label
        infoLabel = QtWidgets.QLabel(
            '<i>Advanced camera properties. Writable properties can be edited. '
            'Click Apply to send changes to camera. Use Refresh to update current values.</i>'
        )
        infoLabel.setWordWrap(True)
        layout.addWidget(infoLabel)
        
        # Refresh button
        self.refreshButton = guitools.BetterPushButton('Refresh Properties')
        layout.addWidget(self.refreshButton)
        
        # Properties table
        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(['Property', 'Current Value', 'New Value', 'Apply', 'Range/Options', 'Access'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)  # Disable sorting because we have widgets in cells
        
        # Set column widths
        self.table.setColumnWidth(0, 200)  # Property name
        self.table.setColumnWidth(1, 100)  # Current value
        self.table.setColumnWidth(2, 150)  # New value (edit control)
        self.table.setColumnWidth(3, 60)   # Apply button
        self.table.setColumnWidth(4, 200)  # Range/Options info
        
        layout.addWidget(self.table)
        
        # Connect signals
        self.refreshButton.clicked.connect(self.sigRefreshClicked)
    
    def _createEditWidget(self, prop):
        """
        Create appropriate edit widget based on property type and metadata.
        
        Args:
            prop: Property dict with metadata
            
        Returns:
            QWidget or None: Edit widget for writable properties, None for read-only
        """
        if not prop.get('writable', False) or prop.get('error'):
            return None
        
        prop_type = prop.get('type', 'NONE')
        text_options = prop.get('text_options')
        prop_range = prop.get('range')
        current_value = prop.get('value')
        
        # MODE properties with text options -> QComboBox
        if text_options:
            combo = QtWidgets.QComboBox()
            combo.setObjectName(prop.get('name', ''))
            
            # Add options to combo box
            for text_key, numeric_value in text_options.items():
                # Decode bytes to string
                if isinstance(text_key, bytes):
                    display_text = text_key.decode('utf-8', errors='replace')
                else:
                    display_text = str(text_key)
                combo.addItem(display_text, numeric_value)
            
            # Set current value if available
            if current_value is not None:
                index = combo.findData(current_value)
                if index >= 0:
                    combo.setCurrentIndex(index)
            
            return combo
        
        # Numeric properties with range -> Spinbox
        elif prop_range and len(prop_range) == 2:
            min_val, max_val = prop_range
            
            # Check if values are integers
            is_integer = (
                isinstance(current_value, int) and
                isinstance(min_val, (int, float)) and
                isinstance(max_val, (int, float)) and
                abs(min_val - int(min_val)) < 1e-9 and
                abs(max_val - int(max_val)) < 1e-9 and
                abs(max_val - min_val) < 1000000  # Reasonable range for spinbox
            )
            
            if is_integer:
                # Use QSpinBox for integer values
                spinbox = QtWidgets.QSpinBox()
                spinbox.setRange(int(min_val), int(max_val))
                if current_value is not None:
                    spinbox.setValue(int(current_value))
                spinbox.setObjectName(prop.get('name', ''))
                return spinbox
            else:
                # Use QDoubleSpinBox for float values
                spinbox = QtWidgets.QDoubleSpinBox()
                spinbox.setRange(min_val, max_val)
                
                # Set reasonable decimals based on range
                range_magnitude = max_val - min_val
                if range_magnitude > 100:
                    spinbox.setDecimals(2)
                elif range_magnitude > 1:
                    spinbox.setDecimals(4)
                else:
                    spinbox.setDecimals(6)
                
                # Set step size
                if range_magnitude > 100:
                    spinbox.setSingleStep(1.0)
                elif range_magnitude > 1:
                    spinbox.setSingleStep(0.1)
                else:
                    spinbox.setSingleStep(range_magnitude / 100)
                
                if current_value is not None:
                    spinbox.setValue(float(current_value))
                
                spinbox.setObjectName(prop.get('name', ''))
                return spinbox
        
        # Unknown writable property -> Keep read-only for safety
        return None
    
    def setProperties(self, properties):
        """
        Populate table with property information and edit controls.
        
        Args:
            properties: List of property dicts from manager.getAdvancedPropertyInfo()
        """
        self._properties = properties
        self.table.setRowCount(len(properties))
        
        for row, prop in enumerate(properties):
            prop_name = prop.get('name', '')
            value = prop.get('value')
            writable = prop.get('writable', False)
            readable = prop.get('readable', False)
            error = prop.get('error')
            
            # Column 0: Property name
            nameItem = QtWidgets.QTableWidgetItem(prop_name)
            self.table.setItem(row, 0, nameItem)
            
            # Column 1: Current value
            if value is None:
                valueStr = '—'
            elif isinstance(value, float):
                valueStr = f'{value:.6g}'
            else:
                valueStr = str(value)
            valueItem = QtWidgets.QTableWidgetItem(valueStr)
            self.table.setItem(row, 1, valueItem)
            
            # Column 2: Edit widget (for writable properties)
            if writable and not error:
                edit_widget = self._createEditWidget(prop)
                if edit_widget:
                    self.table.setCellWidget(row, 2, edit_widget)
                else:
                    # Writable but no safe widget available
                    noEditItem = QtWidgets.QTableWidgetItem('(manual edit unsafe)')
                    noEditItem.setForeground(QtGui.QBrush(QtGui.QColor(150, 150, 150)))
                    self.table.setItem(row, 2, noEditItem)
            else:
                # Read-only
                readOnlyItem = QtWidgets.QTableWidgetItem('—')
                readOnlyItem.setForeground(QtGui.QBrush(QtGui.QColor(150, 150, 150)))
                self.table.setItem(row, 2, readOnlyItem)
            
            # Column 3: Apply button (for writable properties with edit widget)
            if writable and not error and self.table.cellWidget(row, 2):
                applyBtn = guitools.BetterPushButton('Apply')
                applyBtn.setMaximumWidth(60)
                # Connect with lambda that captures current row
                applyBtn.clicked.connect(
                    lambda checked=False, r=row: self._onApplyClicked(r)
                )
                self.table.setCellWidget(row, 3, applyBtn)
            else:
                emptyItem = QtWidgets.QTableWidgetItem('')
                self.table.setItem(row, 3, emptyItem)
            
            # Column 4: Range/Options info
            text_options = prop.get('text_options')
            prop_range = prop.get('range')
            
            if text_options:
                # Show text options
                options = []
                for key in list(text_options.keys())[:3]:
                    if isinstance(key, bytes):
                        options.append(key.decode('utf-8', errors='replace'))
                    else:
                        options.append(str(key))
                
                if len(text_options) > 3:
                    infoStr = ', '.join(options) + f'... (+{len(text_options)-3} more)'
                else:
                    infoStr = ', '.join(options)
            elif prop_range and len(prop_range) == 2:
                # Show numeric range
                infoStr = f'[{prop_range[0]:.6g}, {prop_range[1]:.6g}]'
            else:
                infoStr = '—'
            
            infoItem = QtWidgets.QTableWidgetItem(infoStr)
            self.table.setItem(row, 4, infoItem)
            
            # Column 5: Access (R/W)
            access = ''
            if readable:
                access += 'R'
            if writable:
                access += 'W'
            if not access:
                access = '—'
            accessItem = QtWidgets.QTableWidgetItem(access)
            
            # Color-code writable properties
            if writable:
                accessItem.setForeground(QtGui.QBrush(QtGui.QColor(0, 100, 0)))  # Dark green
                accessItem.setFont(QtGui.QFont('', -1, QtGui.QFont.Bold))
            
            self.table.setItem(row, 5, accessItem)
            
            # Mark rows with errors
            if error:
                for col in range(6):
                    item = self.table.item(row, col)
                    if item:
                        item.setForeground(QtGui.QBrush(QtGui.QColor(150, 150, 150)))  # Gray out
                        item.setToolTip(f'Error: {error}')
        
        self.table.resizeRowsToContents()
    
    def _onApplyClicked(self, row):
        """
        Handle Apply button click for a specific property.
        
        Args:
            row: Table row index
        """
        if row >= len(self._properties):
            return
        
        prop = self._properties[row]
        prop_name = prop.get('name', '')
        
        # Get the edit widget
        edit_widget = self.table.cellWidget(row, 2)
        if not edit_widget:
            return
        
        # Extract value from edit widget
        if isinstance(edit_widget, QtWidgets.QComboBox):
            # Get the data (numeric value) from selected item
            new_value = edit_widget.currentData()
        elif isinstance(edit_widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            new_value = edit_widget.value()
        else:
            return
        
        # Emit signal with property name and new value
        self.sigPropertyChangeRequested.emit(prop_name, new_value)
    
    def showMessage(self, message):
        """
        Show a message in place of the table (e.g., for errors or "not supported").
        
        Args:
            message: Message text to display
        """
        self.table.setRowCount(1)
        self.table.setColumnCount(1)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        
        messageItem = QtWidgets.QTableWidgetItem(message)
        messageItem.setTextAlignment(QtCore.Qt.AlignCenter)
        messageItem.setFlags(QtCore.Qt.ItemIsEnabled)
        self.table.setItem(0, 0, messageItem)


class SettingsWidget(Widget):
    """ Detector settings and ROI parameters. """

    sigROIChanged = QtCore.Signal()
    sigDetectorChanged = QtCore.Signal(str)  # (detectorName)
    sigNextDetectorClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Graphical elements
        detectorTitle = QtWidgets.QLabel('<h2><strong>Detector settings</strong></h2>')
        detectorTitle.setTextFormat(QtCore.Qt.RichText)
        self.ROI = naparitools.NapariROIOverlay()
        self.stack = QtWidgets.QStackedWidget()
        self.trees = {}
        self.stackWidgets = {}  # maps detectorName → widget actually in the stack
        self.advancedWidgets = {}  # Store advanced property widgets by detector name

        self.detectorListBox = QtWidgets.QHBoxLayout()
        self.detectorListLabel = QtWidgets.QLabel('Current detector:')
        self.detectorList = QtWidgets.QComboBox()
        self.nextDetectorButton = guitools.BetterPushButton('Next')
        self.nextDetectorButton.hide()
        self.detectorListBox.addWidget(self.detectorListLabel)
        self.detectorListBox.addWidget(self.detectorList, 1)
        self.detectorListBox.addWidget(self.nextDetectorButton)

        # Add elements to GridLayout
        self.layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.layout)
        self.layout.addWidget(detectorTitle)
        self.layout.addWidget(self.stack)
        self.layout.addLayout(self.detectorListBox)

        # Connect signals
        self.ROI.sigROIChanged.connect(self.sigROIChanged)
        self.detectorList.currentIndexChanged.connect(
            lambda index: self.sigDetectorChanged.emit(self.detectorList.itemData(index))
        )
        self.nextDetectorButton.clicked.connect(self.sigNextDetectorClicked)

    def addDetector(self, detectorName, detectorModel, detectorParameters, detectorActions,
                    supportedBinnings, roiInfos, supportsAdvancedProperties=False):
        """
        Add a detector to the settings widget.
        
        Args:
            detectorName: Name of the detector
            detectorModel: Model string of the detector
            detectorParameters: Dict of detector parameters
            detectorActions: Dict of detector actions
            supportedBinnings: List of supported binning values
            roiInfos: Dict of ROI information
            supportsAdvancedProperties: If True, add an "Advanced" tab for property introspection
        """
        # Create the parameter tree
        paramTree = CamParamTree(detectorParameters, detectorActions,
                                supportedBinnings, roiInfos)
        self.trees[detectorName] = paramTree
        
        # If advanced properties are supported, create a tab widget
        if supportsAdvancedProperties:
            tabWidget = QtWidgets.QTabWidget()

            # Add Basic tab with parameter tree
            tabWidget.addTab(paramTree, 'Basic')

            # Add Advanced tab with property introspection widget
            advancedWidget = AdvancedPropertiesWidget()
            self.advancedWidgets[detectorName] = advancedWidget
            tabWidget.addTab(advancedWidget, 'Advanced')

            # Add tab widget to stack
            self.stack.addWidget(tabWidget)
            self.stackWidgets[detectorName] = tabWidget
        else:
            # No advanced properties, just add the parameter tree directly
            self.stack.addWidget(paramTree)
            self.stackWidgets[detectorName] = paramTree
            self.advancedWidgets[detectorName] = None

        self.detectorList.addItem(f'{detectorModel} ({detectorName})', detectorName)
        self.nextDetectorButton.setVisible(True)

    def setDisplayedDetector(self, detectorName):
        # Scroll bars live on the CamParamTree, not on the QTabWidget wrapper.
        # Use self.trees (always the param tree) for scroll state, and
        # self.stackWidgets for the actual widget to show in the stack.
        prevParamTree = self.stack.currentWidget()
        if isinstance(prevParamTree, QtWidgets.QTabWidget):
            prevParamTree = prevParamTree.widget(0)  # Basic tab = param tree
        scrollX = prevParamTree.horizontalScrollBar().value()
        scrollY = prevParamTree.verticalScrollBar().value()

        self.stack.setCurrentWidget(self.stackWidgets[detectorName])

        newParamTree = self.trees[detectorName]
        newParamTree.horizontalScrollBar().setValue(scrollX)
        newParamTree.verticalScrollBar().setValue(scrollY)

    def selectNextDetector(self):
        self.detectorList.setCurrentIndex(
            (self.detectorList.currentIndex() + 1) % self.detectorList.count()
        )

    def setImageFrameVisible(self, visible):
        """ Sets whether the image frame settings are visible. """
        # For advanced detectors the stack contains a QTabWidget that wraps the
        # CamParamTree on tab 0; for simple detectors the stack holds the
        # CamParamTree directly. setImageFrameVisible only lives on the
        # CamParamTree, so unwrap the tab widget when present.
        current = self.stack.currentWidget()
        if isinstance(current, QtWidgets.QTabWidget):
            current = current.widget(0)
        current.setImageFrameVisible(visible)

    def getROIGraphicsItem(self):
        return self.ROI

    def showROI(self, position=None, size=None):
        if position is not None:
            self.ROI.position = position
        if size is not None:
            self.ROI.size = size
        self.ROI.show()

    def hideROI(self):
        self.ROI.hide()

    def getAdvancedWidget(self, detectorName):
        """
        Get the advanced properties widget for a detector, if it exists.
        
        Args:
            detectorName: Name of the detector
            
        Returns:
            AdvancedPropertiesWidget or None if detector doesn't support advanced properties
        """
        return self.advancedWidgets.get(detectorName)
    
    def hasAdvancedWidget(self, detectorName):
        """
        Check if a detector has an advanced properties widget.
        
        Args:
            detectorName: Name of the detector
            
        Returns:
            bool: True if detector has advanced properties support
        """
        return detectorName in self.advancedWidgets and self.advancedWidgets[detectorName] is not None
    
    @shortcut("Ctrl+N", "Next detector")
    def toggleNextButton(self):
        self.nextDetectorButton.click()


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
