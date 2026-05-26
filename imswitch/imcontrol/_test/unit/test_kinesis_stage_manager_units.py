from imswitch.imcontrol.model.SetupInfo import PositionerInfo
from imswitch.imcontrol.model.managers.positioners.KinesisStageManager import (
    KinesisStageManager,
)

KIRALUX_SETUP_PATH = (
    'imswitch/_data/user_defaults/imcontrol_setups/example_kiralux_teensy.json'
)


class _FakeStage:
    def __init__(self):
        self.positions = {1: 0.0, 2: 0.0}
        self.move_to_calls = []
        self.move_by_calls = []

    def get_position(self, channel):
        return self.positions[channel]

    def move_to(self, position, channel):
        self.move_to_calls.append((position, channel))
        self.positions[channel] = position

    def move_by(self, delta, channel):
        self.move_by_calls.append((delta, channel))
        self.positions[channel] += delta

    def close(self):
        pass


def _positioner_info(manager_properties):
    return PositionerInfo(
        analogChannel=None,
        digitalLine=None,
        managerName='KinesisStageManager',
        managerProperties=manager_properties,
        axes=['X', 'Y'],
        forPositioning=True,
    )


def test_kinesis_stage_scaling_uses_neutral_driver_units(monkeypatch):
    stage = _FakeStage()
    monkeypatch.setattr(
        KinesisStageManager,
        '_getStageObj',
        lambda self, snr, scale, is_rack_system: stage,
    )

    manager = KinesisStageManager(
        _positioner_info({
            'snr': 'MOCK_MLS203',
            'driverUnitsPerPositionUnit': 10.0,
        }),
        'XY',
    )

    manager.setPosition(2.5, 'X')
    manager.move(0.5, 'X')

    assert stage.move_to_calls == [(25.0, 1)]
    assert stage.move_by_calls == [(5.0, 1)]
    assert manager.position['X'] == 3.0


def test_kinesis_stage_rejects_zero_scaling(monkeypatch):
    monkeypatch.setattr(
        KinesisStageManager,
        '_getStageObj',
        lambda self, snr, scale, is_rack_system: _FakeStage(),
    )

    try:
        KinesisStageManager(
            _positioner_info({
                'snr': 'MOCK_MLS203',
                'driverUnitsPerPositionUnit': 0,
            }),
            'XY',
        )
    except ValueError as exc:
        assert 'driverUnitsPerPositionUnit' in str(exc)
    else:
        raise AssertionError('Expected zero scaling to be rejected.')


def test_kiralux_kinesis_stage_does_not_reset_on_close():
    import json

    with open(KIRALUX_SETUP_PATH, encoding='utf-8') as setup_file:
        setup = json.load(setup_file)

    xy_stage = setup['positioners']['XY']

    assert xy_stage['managerName'] == 'KinesisStageManager'
    assert xy_stage['resetOnClose'] is False
