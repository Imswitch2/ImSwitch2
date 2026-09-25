"""The Files panel opens text-like files in the editor, JSON included.

It opened only what mimetypes files under text/*, and .json is
application/json -- so the scan-settings files the scanning tutorials load
could not be opened from the Scripting tab to change, say, the camera name.
"""

import types

import pytest

from imswitch.imscripting.controller.FilesController import (
    FilesController,
    isEditableTextFile,
)


@pytest.mark.parametrize('name', [
    'scan_params/camera_scan_1um.json', '01_hello_imswitch.py', 'README.md',
    'notes.txt', 'values.csv', 'workflow.yaml', 'SETTINGS.JSON',
])
def test_text_files_are_editable(name):
    assert isEditableTextFile(name)


@pytest.mark.parametrize('name', ['grid.tif', 'recording.hdf5', 'image.png',
                                  'module.pyc', 'archive.zip', 'no_extension'])
def test_binary_and_unknown_files_are_not(name):
    assert not isEditableTextFile(name)


def test_double_clicking_a_json_file_opens_it():
    opened = []
    controller = types.SimpleNamespace(_commChannel=types.SimpleNamespace(
        sigOpenFileFromPath=types.SimpleNamespace(emit=opened.append)))

    FilesController.checkAndOpenItem(controller, '/scripts/scan_params/camera_scan_1um.json')
    FilesController.checkAndOpenItem(controller, '/scripts/grid.tif')

    assert opened == ['/scripts/scan_params/camera_scan_1um.json']
