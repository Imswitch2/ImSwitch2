"""The memory-recording policy is kept per computer, beside the default folders."""

import json

from imswitch.improcess.model import memory_recording_preferences as prefs


def test_missing_file_means_list_only(tmp_path):
    assert prefs.load_memory_recording_policy(tmp_path / 'missing.json') == 'list'


def test_round_trip_keeps_other_keys(tmp_path):
    path = tmp_path / 'improcess_options.json'
    path.write_text(json.dumps({'dataFolder': '/data', 'later': {'x': 1}}))

    prefs.save_memory_recording_policy('reconstruct', path)

    content = json.loads(path.read_text())
    assert content['dataFolder'] == '/data'
    assert content['later'] == {'x': 1}
    assert content[prefs.KEY] == 'reconstruct'
    assert prefs.load_memory_recording_policy(path) == 'reconstruct'


def test_unknown_values_fall_back_to_the_default(tmp_path):
    path = tmp_path / 'improcess_options.json'
    path.write_text(json.dumps({prefs.KEY: 'sometimes'}))
    assert prefs.load_memory_recording_policy(path) == 'list'
    assert prefs.normalize_policy(' Current ') == 'current'
    assert prefs.normalize_policy(None) == 'list'


def test_unreadable_file_is_reported_not_raised(tmp_path):
    path = tmp_path / 'improcess_options.json'
    path.write_text('not json')
    assert prefs.load_memory_recording_policy(path) == 'list'

    prefs.save_memory_recording_policy('current', path)
    assert json.loads(path.read_text()) == {prefs.KEY: 'current'}
