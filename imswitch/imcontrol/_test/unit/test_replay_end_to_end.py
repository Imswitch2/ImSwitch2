"""End to end: record on one shipped mock setup, load into other setups.

Starts the real ImControl offscreen (``_replay_e2e_driver``, in a subprocess
so the booted applications leave no process-wide state behind for other
tests), snaps an HDF5 file and a Zarr store on ``simple_mock_setup`` (with a
laser switched on and one value inside the source range), then loads both
through the real File-menu handlers into a variant of that setup with
renamed devices and a narrower laser range, into ``mock_scan_setup`` and
into ``example_mock``, with every hardware manager wrapped in a spy.
"""
import json
import os
import site
import subprocess
import sys

import pytest

from imswitch.imcontrol.model import replay

_DRIVER_MODULE = 'imswitch.imcontrol._test.unit._replay_e2e_driver'
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 4)))
_MOVES = ('setPosition', 'move', 'move_abs', 'move_rel', 'move_to')


@pytest.fixture(scope='module')
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp('replay_e2e')
    # A throwaway HOME keeps the booted ImSwitch out of the user's own
    # ImSwitchConfig; the user site-packages stay where they were.
    env = dict(os.environ, HOME=str(out / 'home'), QT_QPA_PLATFORM='offscreen',
               MPLBACKEND='Agg', PYTHONUSERBASE=site.getuserbase(),
               PYTHONPATH=_REPO + os.pathsep + os.environ.get('PYTHONPATH', ''))
    os.makedirs(env['HOME'], exist_ok=True)
    proc = subprocess.run([sys.executable, '-m', _DRIVER_MODULE, str(out)], env=env, cwd=_REPO,
                          capture_output=True, text=True, timeout=170)
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith('REPORT '))
    return json.loads(line[len('REPORT '):])


def _runs(report):
    for target in ('variant', 'mock_scan_setup', 'example_mock'):
        for mode, run in report[target].items():
            if mode != 'afterProvenance':
                yield target, mode, run


def test_no_exception_escapes_and_nothing_moves_before_apply(report):
    assert report['excepthook'] == []
    for target, mode, run in _runs(report):
        assert run['error'] is None, (target, mode, run['error'])
        assert run['callsAtAsk'] == [], (target, mode)       # nothing before Apply
        assert run['result']['failed'] == [], (target, mode)


def test_emission_is_never_switched_on(report):
    assert report['sourceAttrs']['Laser:405 (ON):Enabled'] in (True, 'True')
    for target, mode, run in _runs(report):
        for device, method, args in run['callsAfter']:
            assert not (method == 'setEnabled' and args and args[0] == 'True'), (target, mode)


def test_stages_move_only_with_the_opt_in(report):
    for target, mode, run in _runs(report):
        moved = [c for c in run['callsAfter'] if c[1] in _MOVES]
        if mode == 'moves':
            assert ['Focus', 'setPosition', ['3.0', "'Z'"]] in moved
        else:
            assert moved == [], (target, mode)


def test_remapped_and_out_of_range_lasers_in_the_variant(report):
    run = report['variant']['hdf5']
    # '488 (EXC)' is gone; 'Ex488' has the same wavelength and is pre-selected.
    assert run['items']['laser:488 (EXC)'] == [replay.MISSING, 'Ex488']
    assert ['Ex488', 'setValue', ['42.0']] in run['callsAfter']
    # 150 is outside the variant's 0-100 for '561 (EXC)': skipped, not clamped.
    assert not [c for c in run['callsAfter'] if c[0] == '561 (EXC)' and c[1] == 'setValue']
    skipped = {(s['item'], s['key']): s['reason'] for s in run['result']['skipped']}
    assert 'Outside' in skipped[('laser:561 (EXC)', 'Value')]


def test_foreign_setups_apply_only_what_they_have(report):
    for target in ('mock_scan_setup', 'example_mock'):
        run = report[target]['hdf5']
        assert run['items']['laser:488 (EXC)'][0] == replay.NO_SUCH_KIND
        assert not [c for c in run['callsAfter'] if c[1] == 'setValue']


def test_a_recording_after_replay_carries_its_provenance(report):
    prov = report['variant']['afterProvenance']
    assert prov['replay:source_file'] == os.path.basename(report['source']['h5'])
    assert prov['replay:source_setup_name'] == 'simple_mock_setup.json'
    mapping = {m['id']: m for m in json.loads(prov['replay:mapping'])}
    assert mapping['laser:488 (EXC)']['target'] == 'Ex488'
    assert 'Value' in mapping['laser:561 (EXC)']['skipped']


def test_new_metadata_is_recorded_without_sensitive_values(report):
    attrs = report['sourceAttrs']
    assert attrs['metadata:schema_version'] in (1, '1')
    assert attrs['setup:hash'].startswith('sha256:')
    assert attrs['Laser:488 (EXC):Units'] == 'V'
    inventory = attrs['setup:inventory']
    assert inventory['lasers']['488 (EXC)']['wavelength'] == 488

    secrets = []

    def collect(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if replay.isSensitiveField(k) and isinstance(v, (str, int)) and len(str(v)) >= 3:
                    secrets.append(str(v))
                collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)
    collect(report['source']['setup'])
    newMetadata = json.dumps({k: v for k, v in attrs.items()
                              if k.split(':')[0] in ('setup', 'metadata', 'replay')})
    assert [s for s in secrets if s in newMetadata] == []
    # File-size impact of the new metadata for this setup (one copy per detector group).
    assert len(newMetadata) < 16384
