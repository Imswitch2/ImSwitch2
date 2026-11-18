"""The deformable mirror manager: its files, its limits, its modes, and a
mirror that is not there."""

import zipfile

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import DeformableMirrorInfo
from imswitch.imcontrol.model.devices.status import (
    DeviceFailureKind, DeviceNotConnectedError, DeviceRuntimeMode,
)
from imswitch.imcontrol.model.managers.DeformableMirrorsManager import DeformableMirrorsManager
from imswitch.imcontrol.model.managers.deformableMirrors import mirror_files
from imswitch.imcontrol.model.resources import (
    ResourceRegistry, ResourceReservedError, deformable_mirror_key, set_resource_registry,
)


def _mirror(**properties):
    properties.setdefault('serialNumber', 'MOCK1')
    properties.setdefault('actuators', 6)
    manager = DeformableMirrorsManager({'DM': DeformableMirrorInfo(
        managerName='AlpaoDMManager', managerProperties=properties)})
    return manager['DM']


def _driver(mirror):
    return mirror.backendHolder.backend


def _xlsx(path, header, values):
    """A minimal .xlsx as pandas writes Microscope_GUI's flat files: shared
    strings for the header, numbers inline."""
    shared = ''.join(f'<si><t>{h}</t></si>' for h in header)
    rows = ['<row r="1">' + ''.join(
        f'<c r="{"AB"[i]}1" t="s"><v>{i}</v></c>' for i in range(len(header))) + '</row>']
    for n, value in enumerate(values, start=2):
        rows.append(f'<row r="{n}"><c r="A{n}"><v>{n - 2}</v></c><c r="B{n}"><v>{value!r}</v></c></row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml', f'<sst {ns}>{shared}</sst>')
        archive.writestr('xl/worksheets/sheet1.xml',
                         f'<worksheet {ns}><sheetData>{"".join(rows)}</sheetData></worksheet>')
    return path


def test_a_mock_mirror_keeps_what_it_was_sent():
    mirror = _mirror()
    assert (mirror.actuatorCount, mirror.runtimeMode, mirror.isSimulated) == (
        6, DeviceRuntimeMode.MOCK, True)
    sent = mirror.poke(2, 0.25)
    assert sent.tolist() == [0, 0, 0.25, 0, 0, 0]
    assert _driver(mirror).last_command.tolist() == sent.tolist()
    assert mirror.command.tolist() == sent.tolist()
    mirror.reset()
    assert mirror.command.tolist() == [0.0] * 6


def test_a_command_beyond_the_limit_is_refused_and_nothing_is_sent():
    mirror = _mirror(maxCommand=0.5)
    mirror.setActuators([0.1] * 6)
    with pytest.raises(ValueError, match=r'actuator 4 at \+0\.600'):
        mirror.setActuators([0, 0, 0, 0, 0.6, 0])
    with pytest.raises(ValueError, match='not finite'):
        mirror.setActuators([0, 0, np.nan, 0, 0, 0])
    with pytest.raises(ValueError, match='6 actuators, not 5'):
        mirror.setActuators([0] * 5)
    assert _driver(mirror).last_command.tolist() == [0.1] * 6
    assert _driver(mirror).sent == 1


def test_the_flat_comes_from_a_csv_an_npy_or_the_old_guis_xlsx(tmp_path):
    flat = [0.01, -0.02, 0.03, 0.0, 0.05, -0.06]
    csv = tmp_path / 'flat.csv'
    csv.write_text('Actuator,Push\n' + '\n'.join(f'{i},{v}' for i, v in enumerate(flat)))
    npy = tmp_path / 'flat.npy'
    np.save(npy, np.array(flat))
    xlsx = _xlsx(tmp_path / 'flat.xlsx', ['Actuator', 'Push'], flat)
    for path in (csv, npy, xlsx):
        mirror = _mirror(flatFile=str(path))
        assert mirror.flatten().tolist() == pytest.approx(flat), path.name
    with pytest.raises(ValueError, match='6 values for 7 actuators'):
        _mirror(flatFile=str(csv), actuators=7)


def test_a_flat_saved_and_read_back_is_the_same_command(tmp_path):
    mirror = _mirror()
    mirror.setActuators([0.1, 0.2, -0.3, 0.0, 1e-7, -0.5])
    mirror.setFlat()
    path = mirror.saveCommand(tmp_path / 'new_flat.csv', mirror.flat)
    assert mirror_files.read_command(path, 6).tolist() == mirror.flat.tolist()
    mirror.zero()
    assert mirror.flatten().tolist() == [0.1, 0.2, -0.3, 0.0, 1e-7, -0.5]


def test_modes_are_the_flat_plus_matrix_rows_by_their_number(tmp_path):
    flat = np.array([0.01] * 6)
    np.save(tmp_path / 'flat.npy', flat)
    modes = np.arange(18, dtype=float).reshape(3, 6) / 100     # 3 modes x 6 actuators
    mirror_files.write_matrix(tmp_path / 'z2c.csv', modes.T)   # stored transposed
    mirror = _mirror(flatFile=str(tmp_path / 'flat.npy'), modesFile=str(tmp_path / 'z2c.csv'),
                     firstMode=2)
    assert mirror.modeNumbers == [2, 3, 4]
    sent = mirror.setModes({3: 2.0, 4: -1.0})
    assert sent == pytest.approx(flat + 2.0 * modes[1] - modes[2])
    assert mirror.setModes([1.0]) == pytest.approx(flat + modes[0])
    with pytest.raises(KeyError, match='modes 2-4, not 5'):
        mirror.setModes({5: 1.0})
    assert mirror.modeCommand({2: 1.0}, base=np.zeros(6)) == pytest.approx(modes[0])


def test_a_mode_matrix_that_fits_no_side_is_refused(tmp_path):
    mirror_files.write_matrix(tmp_path / 'bad.csv', np.zeros((4, 5)))
    with pytest.raises(ValueError, match='neither side is the 6 actuators'):
        _mirror(modesFile=str(tmp_path / 'bad.csv'))


def test_a_mirror_without_the_sdk_is_not_connected_and_refuses_commands(tmp_path):
    mirror = _mirror(serialNumber='BAX1', sdkPath=str(tmp_path / 'no-sdk'))
    assert mirror.isSimulated
    assert mirror.connectionFailureKind is DeviceFailureKind.MISSING_DEPENDENCY
    assert 'ALPAO SDK not found' in mirror.connectionStatusDetails
    with pytest.raises(DeviceNotConnectedError):
        mirror.flatten()
    with pytest.raises(DeviceNotConnectedError):
        mirror.reset()


def test_a_mock_on_failure_stands_in_for_a_missing_mirror(tmp_path):
    mirror = _mirror(serialNumber='BAX1', sdkPath=str(tmp_path / 'no-sdk'),
                     useMockOnFailure=True)
    assert mirror.runtimeMode is DeviceRuntimeMode.MOCK
    assert mirror.poke(0, 0.1)[0] == 0.1


def test_a_reserved_mirror_refuses_other_owners():
    previous = set_resource_registry(ResourceRegistry())
    try:
        from imswitch.imcontrol.model.resources import get_resource_registry
        mirror = _mirror()
        reservation = get_resource_registry().reserve([deformable_mirror_key('DM')], 'script')
        with pytest.raises(ResourceReservedError):
            mirror.flatten()
        mirror.flatten(owner=reservation.token)
        get_resource_registry().release(reservation.token)
        mirror.zero()
    finally:
        set_resource_registry(previous)
