"""The three memory limits: settable per machine, literal until configured.

Contract: a consumer asks for a limit with its own literal as the default and
gets the configured value only once ``configure`` has adopted the options,
so nothing changes for code (or a test) that never configures anything; a
value that cannot be honoured is reported by the setting's name and the
literal stands; and every consumer-facing message can name the setting.
"""
import logging

import pytest

from imswitch.imcommon.model import memory_limits
from imswitch.imcommon.model.memory_limits import MIB


class _Log:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, message, *_a, **_k):
        self.warnings.append(str(message))

    def info(self, message, *_a, **_k):
        self.infos.append(str(message))


def test_unconfigured_every_limit_is_the_callers_literal():
    for limit in memory_limits.SETTING_FIELDS:
        assert memory_limits.configuredBytes(limit) is None
        assert memory_limits.effectiveBytes(limit, 7 * MIB) == 7 * MIB


def test_options_configure_the_limits_in_mib():
    from imswitch.imcontrol.model.Options import MemoryOptions, Options

    options = Options.from_json(
        '{"setupFileName": "x.json", "memory": {"writerQueueMB": 128, '
        '"perDetectorQueueMB": 64, "processingWorkingSetMB": 32}}',
        infer_missing=True,
    )
    assert isinstance(options.memory, MemoryOptions)
    log = _Log()

    adopted = memory_limits.configure(options.memory, logger=log)

    assert adopted == {
        'writerQueueBytes': 128 * MIB,
        'perDetectorQueueBytes': 64 * MIB,
        'processingWorkingSetBytes': 32 * MIB,
    }
    assert memory_limits.effectiveBytes('perDetectorQueueBytes', 256 * MIB) == 64 * MIB
    assert len(log.infos) == 1 and 'perDetectorQueueMB = 64 MiB' in log.infos[0]
    assert log.warnings == []


def test_an_options_file_without_the_group_gets_todays_defaults():
    """Existing options files predate the group; they must load and mean today."""
    from imswitch.imcontrol.model.Options import Options

    options = Options.from_json('{"setupFileName": "x.json"}', infer_missing=True)

    assert (options.memory.writerQueueMB, options.memory.perDetectorQueueMB,
            options.memory.processingWorkingSetMB) == (512, 256, 256)
    memory_limits.configure(options.memory, logger=_Log())
    assert memory_limits.effectiveBytes('writerQueueBytes', 1) == 512 * MIB
    assert memory_limits.effectiveBytes('perDetectorQueueBytes', 1) == 256 * MIB
    assert memory_limits.effectiveBytes('processingWorkingSetBytes', 1) == 256 * MIB


def test_the_group_round_trips_through_the_options_file():
    from imswitch.imcontrol.model.Options import Options

    options = Options.from_json('{"setupFileName": "x.json"}', infer_missing=True)
    again = Options.from_json(options.to_json(), infer_missing=True)
    assert again.memory == options.memory
    assert '"perDetectorQueueMB": 256' in options.to_json()


@pytest.mark.parametrize('bad', [0, -1, 2.5, 'lots', True, None])
def test_a_value_that_cannot_be_honoured_is_named_and_the_literal_stands(bad):
    from types import SimpleNamespace

    log = _Log()
    memory_limits.configure(
        SimpleNamespace(writerQueueMB=bad, perDetectorQueueMB=64, processingWorkingSetMB=None),
        logger=log,
    )

    assert memory_limits.configuredBytes('writerQueueBytes') is None
    assert memory_limits.effectiveBytes('writerQueueBytes', 512 * MIB) == 512 * MIB
    assert memory_limits.effectiveBytes('perDetectorQueueBytes', 1) == 64 * MIB
    if bad is None:
        assert log.warnings == []
    else:
        assert len(log.warnings) == 1
        assert 'memory.writerQueueMB in imcontrol_options.json' in log.warnings[0]


def test_reset_returns_every_consumer_to_its_literal():
    from types import SimpleNamespace

    memory_limits.configure(SimpleNamespace(perDetectorQueueMB=8), logger=_Log())
    assert memory_limits.configuredBytes('perDetectorQueueBytes') == 8 * MIB
    memory_limits.reset()
    assert memory_limits.configuredBytes('perDetectorQueueBytes') is None


def test_setting_refs_name_the_field_and_the_file():
    assert memory_limits.settingRef('perDetectorQueueBytes') == (
        'memory.perDetectorQueueMB in imcontrol_options.json'
    )
    with pytest.raises(KeyError):
        memory_limits.configuredBytes('somethingElse')


def test_frame_budget_note_says_how_many_fit_or_that_one_does_not():
    assert memory_limits.frameBudgetNote(8 * MIB, 256 * MIB) == (
        'about 32 fit in the 256 MiB queue budget'
    )
    note = memory_limits.frameBudgetNote(260 * MIB, 256 * MIB)
    assert note.startswith('exceeds the 256 MiB queue budget on its own')
    assert 'admitted alone' in note
    assert memory_limits.describeBytes(260 * MIB) == '260 MiB'
    assert memory_limits.describeBytes(8 * MIB) == '8.0 MiB'
    assert memory_limits.describeBytes(12 * 1024) == '12 kB'


def test_configure_uses_the_module_logger_when_none_is_given(caplog):
    from types import SimpleNamespace

    with caplog.at_level(logging.INFO, logger='imswitch.imcommon.model.memory_limits'):
        memory_limits.configure(SimpleNamespace(writerQueueMB=64))
    assert any('writerQueueMB = 64 MiB' in record.getMessage() for record in caplog.records)
