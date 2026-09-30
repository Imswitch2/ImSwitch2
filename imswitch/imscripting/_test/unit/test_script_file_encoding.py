"""Scripts are read and written as UTF-8 on every platform.

ScriptEntry used open() without an encoding, i.e. the locale's: on Windows
(cp1252) the UTF-8 "µ" in the shipped tutorials showed up as "Âµ" in the
editor, and saving wrote cp1252 bytes back.
"""

import locale

from imswitch.imscripting.model.ScriptStore import ScriptEntry

TEXT = "STEP_UM = 50.0   # µm, not Âµm\nprint('Δt = 5 µs')\n"


def test_utf8_file_loads_as_utf8(tmp_path):
    path = tmp_path / 'script.py'
    path.write_bytes(TEXT.encode('utf-8'))

    assert ScriptEntry.loadFromFile(str(path)).code == TEXT


def test_save_writes_utf8(tmp_path):
    path = tmp_path / 'script.py'
    entry = ScriptEntry(filePath=str(path), code=TEXT, unsaved=True)

    entry.save()

    assert path.read_bytes() == TEXT.encode('utf-8')
    assert not entry.unsaved


def test_a_byte_order_mark_is_not_part_of_the_code(tmp_path):
    path = tmp_path / 'script.py'
    path.write_bytes(b'\xef\xbb\xbf' + TEXT.encode('utf-8'))

    assert ScriptEntry.loadFromFile(str(path)).code == TEXT


def test_a_script_saved_by_an_older_windows_imswitch_still_opens(tmp_path, monkeypatch):
    # Not valid UTF-8 ("µ" is the single byte 0xB5 in cp1252): read it in the
    # locale's encoding, as it was written.
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda *_a: 'cp1252')
    path = tmp_path / 'old.py'
    path.write_bytes('x = 1  # µm\n'.encode('cp1252'))

    assert ScriptEntry.loadFromFile(str(path)).code == 'x = 1  # µm\n'
