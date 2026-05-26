from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.JenaPiezoZManager import (
    JenaPiezoZManager,
)


class _FakeRS232Manager:
    def __init__(self, initial_position=25.0):
        self.position = initial_position
        self.queries = []
        self.writes = []
        self.external_active = False
        self._settings = {'recv_termination': '\n'}
        self._rs232port = _FakeRS232Port()

    def query(self, command):
        self.queries.append(command)
        if command == 'rd':
            return f'rd, {self.position:.2f}'
        raise TimeoutError(f'No reply for write-only command {command}')

    def write(self, command):
        self.writes.append(command)
        if command == 'i1':
            self.external_active = True
        elif command == 'i0':
            self.external_active = False
        elif command.startswith('wr,'):
            self.position = float(command.split(',', maxsplit=1)[1].strip())


class _FakeRS232sManager(dict):
    pass


class _FakeRS232Port:
    def __init__(self):
        self._resource = _FakeResource()


class _FakeResource:
    def __init__(self):
        self.read_termination = '\n'


def _positioner_info(manager_properties):
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='JenaPiezoZManager',
        managerProperties=manager_properties,
        axes=['Z'],
        forPositioning=True,
        resetOnClose=False,
    )


def test_jena_write_only_commands_do_not_query_for_replies():
    rs232 = _FakeRS232Manager(initial_position=25.0)
    manager = JenaPiezoZManager(
        _positioner_info({
            'rs232device': 'jenaPiezo',
            'posRangeUm': [0, 100],
            'waitForSettle': True,
            'settleToleranceUm': 0.1,
            'settleTimeoutS': 0.2,
        }),
        'Z',
        rs232sManager=_FakeRS232sManager({'jenaPiezo': rs232}),
    )

    manager.setPosition(40.0, 'Z')
    manager.finalize()

    assert rs232.queries == ['rd', 'rd']
    assert rs232.writes == ['cl', 'i1', 'wr, 40.0', 'i0']
    assert manager._position['Z'] == 40.0
    assert rs232._settings['recv_termination'] == '\r'
    assert rs232._rs232port._resource.read_termination == '\r'
