import os

import pytest

from imswitch.imcommon.model.dirtools import DataFileDirs
from imswitch.imcontrol.view.guitools.ViewSetupInfo import ViewSetupInfo


def _example_setup_files():
    setup_dir = os.path.join(DataFileDirs.UserDefaults, 'imcontrol_setups')
    return [
        os.path.join(setup_dir, file_name)
        for file_name in sorted(os.listdir(setup_dir))
        if file_name.endswith('.json')
    ]


@pytest.mark.parametrize('setup_path', _example_setup_files(), ids=os.path.basename)
def test_bundled_example_setup_schema_parses(setup_path):
    with open(setup_path) as setup_file:
        setup_info = ViewSetupInfo.from_json(setup_file.read())

    assert setup_info is not None
