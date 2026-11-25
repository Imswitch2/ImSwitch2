"""Decisions behind the Calibrations window (Hardware → Calibrations…).

The window (``view/widgets/CalibrationsWidget.py``) only displays what this
controller hands it and reports clicks; everything that decides something --
which calibrations belong to which laser, whether one may be activated, what
is written where -- is here, and drivable without the window
(``_test/unit/test_calibrations_controller.py``). Nothing here talks to
hardware directly: activating goes through ``LaserController
.setLaserCalibrationTable``, which owns the swap rules (refused while the
laser is on, setpoint reset to the bottom of the new scale).

* The store is global (``<ImSwitchConfig>/calibrations``); which calibration
  is active for a laser is a fact of the setup. A swap applies to this
  session; *Save selection to setup file* writes the ``calibrationId``
  references (and nothing else) into the setup file.
* A record is offered for a laser only when it matches it (name, wavelength,
  drive unit, manager / identifier where known); others are listed as
  belonging to other devices and need an explicit confirmation; a drive-unit
  mismatch is refused.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcontrol.model import configfiletools
from imswitch.imcontrol.model.calibrations import (
    RAW_UNITS_ID,
    CalibrationError,
    CalibrationStore,
    LaserDescriptor,
    LaserPowerTable,
    export_legacy_csv,
    format_power,
    match_to_laser,
    read_legacy_csv,
    validate_laser_power,
)
from imswitch.imcontrol.model.calibrations.setup_refs import (
    FOLLOW_CSV_PATH,
    describe_selection,
    read_setup_selection,
    selection_from_properties,
    write_setup_selection,
)
from imswitch.imcontrol.model.devices.status import device_usable

from ..basecontrollers import ImConWidgetController
from ._laser_calibration import CalibrationSwapRefused, powerTableOf, resolutionOf


def _setupFilePath():
    options, _ = configfiletools.loadOptions()
    return configfiletools.getSetupFilePath(options.setupFileName)


class CalibrationsController(ImConWidgetController):
    """Feeds the Calibrations window and carries out its actions."""

    def __init__(self, *args, laserController=None, store=None, setupFilePath=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)
        self._laserController = laserController
        self._store = store if store is not None else CalibrationStore()
        self._setupFilePathFn = setupFilePath or _setupFilePath
        #: This session's selection per laser: record id, 'raw', or ''
        #: (calibCsvPath decides) -- starts as what the setup said at startup.
        self._sessionSelection: Dict[str, str] = {}
        for name, manager in self._lasers():
            info = self._setupInfo.lasers.get(name) if hasattr(self._setupInfo, 'lasers') else None
            self._sessionSelection[name] = selection_from_properties(
                getattr(info, 'managerProperties', None))
        self._selected = None          # ('laser', name) | ('calibration', id, laser) | ...
        self._records = {}
        if self._widget is not None:
            self._widget.sigRefreshRequested.connect(self.refresh)
            self._widget.sigItemSelected.connect(self.select)
            self._widget.sigActivateRequested.connect(self.activateSelected)
            self._widget.sigRawRequested.connect(self.runSelectedLaserRaw)
            self._widget.sigImportRequested.connect(self.importCsv)
            self._widget.sigImportReferencedRequested.connect(self.importReferencedCsv)
            self._widget.sigExportRequested.connect(self.exportSelected)
            self._widget.sigSaveAnnotationsRequested.connect(self.saveAnnotations)
            self._widget.sigSaveToSetupRequested.connect(self.saveSelectionToSetup)
        signal = getattr(laserController, 'sigLaserCalibrationChanged', None)
        if signal is not None:
            try:
                signal.connect(lambda _name: self.refresh())
            except (AttributeError, RuntimeError, TypeError):
                pass

    # -- facts -----------------------------------------------------------------

    def _lasers(self):
        try:
            return [(name, manager) for name, manager in self._master.lasersManager]
        except Exception:  # noqa: BLE001 - no lasers in this setup
            return []

    def _manager(self, name):
        return dict(self._lasers()).get(name)

    def _descriptor(self, name) -> LaserDescriptor:
        manager = self._manager(name)
        identifier = ''
        getIdentifier = getattr(manager, 'calibrationDeviceIdentifier', None)
        if callable(getIdentifier):
            try:
                identifier = str(getIdentifier() or '')
            except Exception:  # noqa: BLE001
                identifier = ''
        return LaserDescriptor(
            name=name, wavelengthNm=getattr(manager, 'wavelength', None),
            driveUnit=str(getattr(manager, 'rawValueUnits', '') or ''),
            managerClass=type(manager).__name__, identifier=identifier,
            supportsCalibration=getattr(type(manager), 'supportsPowerCalibration', False) is True,
        )

    def _laserActionsAllowed(self, name) -> str:
        """'' when activate / raw may be offered for ``name``, else why not."""
        manager = self._manager(name)
        if manager is None:
            return f'{name} is not part of this setup'
        if manager.isBinary:
            return f'{name} is switched on and off only'
        if getattr(type(manager), 'supportsPowerCalibration', False) is not True:
            return (f'{name} runs on raw units only: {type(manager).__name__} does not apply '
                    f'power calibrations')
        if not device_usable(manager):
            return f'{name} is not connected'
        if self._laserController is None:
            return 'this setup has no Laser panel to switch calibrations through'
        return ''

    # -- model for the window ---------------------------------------------------

    def model(self) -> Dict[str, Any]:
        """Everything the window shows, as plain data."""
        stored, unreadable = self._store.scan()
        self._records = {s.record.id: s for s in stored}
        setupSays = read_setup_selection(self._setupFilePathFn())
        claimed = set()
        lasers = []
        for name, manager in self._lasers():
            if manager.isBinary:
                continue
            descriptor = self._descriptor(name)
            resolution = resolutionOf(manager)
            table = powerTableOf(manager)
            entries = []
            for record_id, s in self._records.items():
                match = match_to_laser(s.record, descriptor)
                if match.offered:
                    claimed.add(record_id)
                    entries.append({'id': record_id, 'title': s.record.title(),
                                    'active': table is not None and table.id == record_id,
                                    'path': str(s.path)})
            session = self._sessionSelection.get(name, '')
            disk = setupSays.get(name, {})
            csvPath = disk.get('calibCsvPath', '') or str(
                getattr(manager, 'getProperty', lambda *_: '')('calibCsvPath') or '')
            lasers.append({
                'name': name,
                'wavelength': descriptor.wavelengthNm,
                'managerClass': descriptor.managerClass,
                'state': resolution.state,
                'error': resolution.error,
                'units': manager.valueUnits,
                'activeId': table.id if table is not None else None,
                'activeTitle': table.record.title() if table is not None else '',
                'csvPath': csvPath,
                'csvResolved': resolution.resolvedPath if resolution.requested.startswith(
                    'calibCsvPath') else '',
                'csvInForce': table is not None and table.id.startswith('csv:'),
                'session': session,
                'setupFile': disk.get('calibrationId', '') if disk else None,
                'differs': bool(disk) and disk.get('calibrationId', '') != session,
                'blocked': self._laserActionsAllowed(name),
                'entries': entries,
            })
        others = []
        for record_id, s in self._records.items():
            if record_id in claimed:
                continue
            others.append({'id': record_id, 'title': s.record.title(),
                           'madeFor': s.record.device.name or '?',
                           'wavelength': s.record.device.wavelengthNm,
                           'path': str(s.path)})
        return {'lasers': lasers, 'others': others,
                'unreadable': [{'path': str(u.path), 'error': u.error} for u in unreadable],
                'storeRoot': str(self._store.root),
                'setupFile': str(self._setupFilePathFn())}

    def refresh(self) -> None:
        if self._widget is None:
            return
        model = self.model()
        self._widget.setModel(model)
        differing = [laser['name'] for laser in model['lasers'] if laser['differs']]
        self._widget.setSetupBanner(
            ('This session differs from the setup file for: ' + ', '.join(differing)
             + '. Use "Save selection to setup file" to keep it, or it is back at restart.')
            if differing else '')
        self._showSelection()

    # -- selection and details -----------------------------------------------------

    def select(self, item) -> None:
        self._selected = tuple(item) if item else None
        self._showSelection()

    def _selectedLaser(self) -> Optional[str]:
        if not self._selected:
            return None
        if self._selected[0] in ('laser', 'csv'):
            return self._selected[1]
        if self._selected[0] == 'calibration':
            return self._selected[2]
        return None

    def _selectedRecordId(self) -> Optional[str]:
        if self._selected and self._selected[0] == 'calibration':
            return self._selected[1]
        if self._selected and self._selected[0] == 'laser':
            table = powerTableOf(self._manager(self._selected[1]))
            if table is not None and table.id in self._records:
                return table.id
        return None

    def details(self) -> Dict[str, Any]:
        """What the details pane shows for the current selection."""
        laser = self._selectedLaser()
        record_id = self._selectedRecordId()
        info: Dict[str, Any] = {'laser': laser, 'recordId': record_id, 'lines': [],
                                'plot': None, 'editable': False, 'measuredAt': '',
                                'comment': '', 'match': ''}
        record = None
        path = ''
        if record_id is not None and record_id in self._records:
            record = self._records[record_id].record
            path = str(self._records[record_id].path)
            info['editable'] = True
        elif self._selected and self._selected[0] in ('laser', 'csv') and laser:
            table = powerTableOf(self._manager(laser))
            if table is not None:
                record, path = table.record, table.source
        if laser:
            manager = self._manager(laser)
            resolution = resolutionOf(manager)
            info['lines'].append(f'Laser {laser}: '
                                 + ('calibrated, value in %' if resolution.state == 'calibrated'
                                    else f'raw units ({manager.rawValueUnits})'))
            if resolution.failed:
                info['lines'].append(f'Calibration NOT loaded: {resolution.error}')
            if resolution.resolvedPath:
                info['lines'].append(f'Referenced file resolves to: {resolution.resolvedPath}')
            blocked = self._laserActionsAllowed(laser)
            if blocked:
                info['lines'].append(f'No calibration actions: {blocked}.')
        if record is not None:
            if laser:
                match = match_to_laser(record, self._descriptor(laser))
                info['match'] = match.level
                if match.messages:
                    info['lines'].append('Match: ' + match.text())
            rows = record.table
            drive = [r[0] for r in rows]
            power = [r[1] for r in rows]
            prov = record.provenance
            info['lines'] += [
                f'Calibration {record.id}  ({record.shortHash()})',
                f'Maps: {record.input.quantity} [{record.input.unit or "unit not stated"}] → '
                f'{record.output.quantity} [{record.output.unit or "relative, no unit"}]',
                f'Range: {min(drive):g} to {max(drive):g} {record.input.unit}; '
                f'{len(rows)} points; outside it a request is refused',
                f'Power span: {format_power(min(power), record.output.unit) or f"{min(power):g}"}'
                f' to {format_power(max(power), record.output.unit) or f"{max(power):g}"}',
                f'Made for: {record.device.name or "?"}'
                + (f', {record.device.wavelengthNm:g} nm' if record.device.wavelengthNm else '')
                + (f', {record.device.managerClass}' if record.device.managerClass else '')
                + (f', {record.device.identifier}' if record.device.identifier else ''),
                f'Measured at: {record.measuredAt or "not recorded"}',
                f'Comment: {record.comment or "-"}',
                f'Created: {prov.created or "?"} by {prov.tool or "?"}',
                f'Reference instrument: {prov.referenceInstrument or "not recorded"}',
            ]
            if prov.sourceRun:
                info['lines'].append(f'Source run: {prov.sourceRun}')
            if prov.sourceFile:
                info['lines'].append(f'Imported from: {prov.sourceFile}')
            for key, value in prov.header:
                info['lines'].append(f'{key}: {value}')
            info['lines'].append(f'File: {path}')
            problems = validate_laser_power(record)
            if problems:
                info['lines'].append('Cannot be applied: ' + '; '.join(problems))
            info.update(plot={'drive': drive, 'power': power, 'driveUnit': record.input.unit,
                              'powerUnit': record.output.unit},
                        measuredAt=record.measuredAt, comment=record.comment)
        return info

    def _showSelection(self) -> None:
        if self._widget is None:
            return
        info = self.details()
        self._widget.setDetails(info)
        laser = info['laser']
        blocked = self._laserActionsAllowed(laser) if laser else 'select a laser'
        record_id = info['recordId']
        manager = self._manager(laser) if laser else None
        table = powerTableOf(manager) if manager is not None else None
        resolution = resolutionOf(manager) if manager is not None else None
        self._widget.setActions({
            'activate': (not blocked and record_id is not None and info['match'] != 'incompatible'
                         and (table is None or table.id != record_id)),
            'activateLabel': (f'Activate for {laser}' if info['match'] in ('', 'match')
                              else f'Use for {laser} anyway…') if laser else 'Activate',
            'raw': not blocked and table is not None,
            'rawLabel': f'Run {laser} on raw units' if laser else 'Run on raw units',
            'export': record_id is not None or table is not None,
            'importReferenced': bool(resolution is not None and resolution.requested.startswith(
                'calibCsvPath') and table is not None and table.id.startswith('csv:')),
            'saveAnnotations': info['editable'],
            'saveToSetup': any(self._sessionDiffers().values()),
            'blockedReason': blocked or '',
        })

    def _sessionDiffers(self) -> Dict[str, bool]:
        disk = read_setup_selection(self._setupFilePathFn())
        return {name: (name in disk and disk[name].get('calibrationId', '') != value)
                for name, value in self._sessionSelection.items()}

    def _status(self, text, error=False):
        (self.__logger.warning if error else self.__logger.info)(text)
        if self._widget is not None:
            self._widget.setStatus(text, error=error)

    def _confirm(self, title, text) -> bool:
        if self._widget is None:
            return False
        return bool(self._widget.confirm(title, text))

    # -- actions ---------------------------------------------------------------------

    def activateSelected(self) -> None:
        laser, record_id = self._selectedLaser(), self._selectedRecordId()
        if laser and record_id:
            self.activate(laser, record_id)

    def runSelectedLaserRaw(self) -> None:
        laser = self._selectedLaser()
        if laser:
            self.runRaw(laser)

    @APIExport(runOnUIThread=True)
    def activateLaserCalibration(self, laserName: str, calibrationId: str,
                                 allowMismatch: bool = False) -> bool:
        """ Put the stored calibration ``calibrationId`` in force for
        ``laserName`` for this session (refused while the laser is on; its
        setpoint is reset to 0 %). A calibration made for another device is
        refused unless ``allowMismatch``; one with a different drive unit
        always. Returns True when it is in force. """
        return self.activate(laserName, calibrationId, confirmed=bool(allowMismatch),
                             interactive=False)

    def activate(self, laserName, calibrationId, *, confirmed=False, interactive=True) -> bool:
        blocked = self._laserActionsAllowed(laserName)
        if blocked:
            self._status(f'Not activated: {blocked}.', error=True)
            return False
        stored = self._store.get(calibrationId)
        if stored is None:
            self._status(f'No calibration {calibrationId!r} in {self._store.root}.', error=True)
            return False
        match = match_to_laser(stored.record, self._descriptor(laserName))
        if match.blocked:
            self._status(f'Not activated for {laserName}: {match.text()}', error=True)
            return False
        if match.needsConfirmation and not confirmed:
            strong = match.level == 'other_device'
            if not interactive or not self._confirm(
                    'Calibration made for another device' if strong
                    else 'Calibration made for another laser name',
                    match.text() + ('\n\nThe curve may not describe this laser at all. '
                                    if strong else '\n\n')
                    + f'Use it for {laserName} anyway?'):
                self._status(f'Not activated for {laserName}: {match.text()}', error=True)
                return False
        manager = self._manager(laserName)
        try:
            table = LaserPowerTable(stored.record, drive_unit=manager.rawValueUnits,
                                    source=str(stored.path))
            message = self._laserController.setLaserCalibrationTable(
                laserName, table, requested=f'calibration {calibrationId}',
                resolvedPath=str(stored.path))
        except (CalibrationError, CalibrationSwapRefused) as exc:
            self._status(f'Not activated: {exc}', error=True)
            return False
        self._sessionSelection[laserName] = calibrationId
        self._status(message + (f' ({match.text()})' if match.messages else ''))
        self.refresh()
        return True

    @APIExport(runOnUIThread=True)
    def runLaserOnRawUnits(self, laserName: str) -> bool:
        """ Run ``laserName`` without a calibration for this session (its
        value becomes the raw drive, reset to the minimum; refused while the
        laser is on). """
        return self.runRaw(laserName)

    def runRaw(self, laserName) -> bool:
        blocked = self._laserActionsAllowed(laserName)
        if blocked:
            self._status(f'Not changed: {blocked}.', error=True)
            return False
        try:
            message = self._laserController.setLaserCalibrationTable(
                laserName, None, requested='raw units (session)')
        except CalibrationSwapRefused as exc:
            self._status(f'Not changed: {exc}', error=True)
            return False
        self._sessionSelection[laserName] = RAW_UNITS_ID
        self._status(message)
        self.refresh()
        return True

    @APIExport(runOnUIThread=True)
    def importLaserCalibration(self, path: str, laserName: str = '') -> str:
        """ Import a calibration CSV (for example the ``lut_file`` of
        :meth:`measureLaserPowerLut`) into the global calibration store and
        return its id. ``laserName`` names the laser it was made for when
        the file does not say. Nothing is activated. """
        record_id = self.importCsv(path, laserName=laserName or None, interactive=False)
        if record_id is None:
            raise CalibrationError(self._lastError)
        return record_id

    def importCsv(self, path=None, *, laserName=None, interactive=True) -> Optional[str]:
        """Import a CSV into the store; returns the new id (None: not imported)."""
        self._lastError = ''
        if path in (None, False, ''):
            if not interactive or self._widget is None:
                return None
            path = self._widget.askOpenFile()
            if not path:
                return None
        target = laserName or self._selectedLaser()
        manager = self._manager(target) if target else None
        try:
            record = read_legacy_csv(
                path, drive_unit=str(getattr(manager, 'rawValueUnits', '') or ''))
        except (OSError, CalibrationError) as exc:
            return self._importFailed(f'{path}: {exc}')
        if not record.device.name:
            if laserName:
                target = laserName
            elif interactive and self._widget is not None:
                names = [name for name, m in self._lasers() if not m.isBinary]
                target = self._widget.askLaser(
                    'The file does not say which laser it was measured for. Which laser is it?',
                    names, target)
                if not target:
                    return None
            else:
                return self._importFailed(f'{path}: the file does not name its laser; pass '
                                          f'laserName')
        elif laserName is None:
            target = record.device.name
        manager = self._manager(target) if target else None
        if manager is not None:
            descriptor = self._descriptor(target)
            device = record.device
            record = replace(record, device=replace(
                device, name=device.name or target,
                wavelengthNm=device.wavelengthNm if device.wavelengthNm is not None
                else descriptor.wavelengthNm,
                managerClass=device.managerClass or descriptor.managerClass,
                identifier=device.identifier or descriptor.identifier),
                input=replace(record.input, unit=record.input.unit or descriptor.driveUnit))
        elif not record.device.name:
            record = replace(record, device=replace(record.device, name=str(target)))
        problems = validate_laser_power(record)
        if problems:
            return self._importFailed(f'{path} not imported: ' + '; '.join(problems))
        try:
            stored = self._store.add(record)
        except (OSError, CalibrationError) as exc:
            return self._importFailed(f'{path} not imported: {exc}')
        self._status(f'Imported {Path(path).name} as {stored.record.id} ({stored.path.name}); '
                     f'not activated.')
        self.refresh()
        if self._widget is not None:
            self.select(('calibration', stored.record.id, target if manager else None))
        return stored.record.id

    def _importFailed(self, message):
        self._lastError = message
        self._status(message, error=True)
        return None

    def importReferencedCsv(self) -> Optional[str]:
        """Copy the selected laser's calibCsvPath file into the store (the
        file and the setup are left as they are)."""
        laser = self._selectedLaser()
        manager = self._manager(laser) if laser else None
        resolution = resolutionOf(manager) if manager is not None else None
        if resolution is None or not resolution.resolvedPath:
            self._status('The selected laser references no calibCsvPath file.', error=True)
            return None
        return self.importCsv(resolution.resolvedPath, laserName=laser, interactive=True)

    def exportSelected(self, path=None) -> Optional[str]:
        record_id = self._selectedRecordId()
        record = self._records[record_id].record if record_id in self._records else None
        if record is None:
            laser = self._selectedLaser()
            table = powerTableOf(self._manager(laser)) if laser else None
            record = table.record if table is not None else None
        if record is None:
            self._status('Select a calibration to export.', error=True)
            return None
        if not path:
            if self._widget is None:
                return None
            default = (record.id.replace(':', '_') or 'calibration') + '.calib.csv'
            path = self._widget.askSaveFile(default)
            if not path:
                return None
        try:
            with open(path, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(export_legacy_csv(record))
        except (OSError, CalibrationError) as exc:
            self._status(f'Export failed: {exc}', error=True)
            return None
        self._status(f'Exported {record.id} to {path}')
        return str(path)

    def saveAnnotations(self, measuredAt, comment) -> bool:
        record_id = self._selectedRecordId()
        if record_id is None or record_id not in self._records:
            self._status('Only calibrations in the store can be annotated; import the file '
                         'first.', error=True)
            return False
        try:
            stored = self._store.update_annotations(record_id, measuredAt=measuredAt,
                                                    comment=comment)
        except (OSError, CalibrationError) as exc:
            self._status(f'Not saved: {exc}', error=True)
            return False
        # Lasers running on this record show the new texts at once; the
        # content hash is unchanged, so nothing about the drive changes.
        for name, manager in self._lasers():
            table = powerTableOf(manager)
            if table is not None and table.id == record_id \
                    and table.contentHash() == stored.record.contentHash():
                table.record = stored.record
                refresh = getattr(self._laserController, '_refreshLaserCalibrationDisplay', None)
                if callable(refresh):
                    refresh(name)
        self._status(f'Saved location and comment of {record_id}.')
        self.refresh()
        return True

    def saveSelectionToSetup(self, *, confirmed=False) -> bool:
        """Write this session's selection into the setup file: the
        ``calibrationId`` of each laser whose selection differs from it."""
        setupPath = self._setupFilePathFn()
        differs = {name: value for name, value in self._sessionSelection.items()
                   if self._sessionDiffers().get(name)}
        if not differs:
            self._status('The setup file already has this selection.')
            return False
        disk = read_setup_selection(setupPath)
        lines = [f'  {name}: {describe_selection(disk.get(name, {}).get("calibrationId", ""), disk.get(name, {}).get("calibCsvPath", ""))}'
                 f'  →  {describe_selection(value, disk.get(name, {}).get("calibCsvPath", ""))}'
                 for name, value in differs.items()]
        text = (f'Write the active calibration of these lasers into {setupPath}?\n\n'
                + '\n'.join(lines)
                + '\n\nOnly managerProperties.calibrationId of these lasers is changed; the '
                  'rest of the file, and anything else this session has not saved, is left '
                  'as it is on disk. calibCsvPath entries stay.')
        if not confirmed and not self._confirm('Save calibration selection', text):
            return False
        try:
            write_setup_selection(setupPath, differs)
        except (OSError, ValueError, KeyError) as exc:
            self._status(f'Not saved: {exc}', error=True)
            return False
        # Keep the in-memory setup in step, so a later full save (a laser
        # preset, say) writes the same reference instead of undoing it.
        for name, value in differs.items():
            info = self._setupInfo.lasers.get(name)
            props = getattr(info, 'managerProperties', None)
            if isinstance(props, dict):
                if value == FOLLOW_CSV_PATH:
                    props.pop('calibrationId', None)
                else:
                    props['calibrationId'] = value
        self._status(f'Saved the calibration selection of {", ".join(differs)} to {setupPath}.')
        self.refresh()
        return True

    @APIExport()
    def getStoredCalibrations(self) -> List[Dict[str, Any]]:
        """ The calibrations in the global store: ``id``, ``laser`` (made
        for), ``wavelength``, ``measuredAt``, ``comment``, ``created``,
        ``contentHash`` and ``path``. """
        return [{'id': s.record.id, 'laser': s.record.device.name,
                 'wavelength': s.record.device.wavelengthNm,
                 'measuredAt': s.record.measuredAt, 'comment': s.record.comment,
                 'created': s.record.provenance.created,
                 'contentHash': s.record.contentHash(), 'path': str(s.path)}
                for s in self._store.list()]
