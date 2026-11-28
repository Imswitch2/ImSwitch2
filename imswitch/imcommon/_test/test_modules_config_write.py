"""modules.json is never left truncated: it is read at import by every
ImSwitch (and every test worker) that starts."""
import json
import os

from imswitch.imcommon.model import modulesconfigtools as tools


def test_the_file_is_replaced_in_one_step_and_only_when_it_changes(tmp_path, monkeypatch):
    path = tmp_path / 'modules.json'
    monkeypatch.setattr(tools, '_modulesFilePath', str(path))
    monkeypatch.setattr(tools, '_modules', tools._Modules(enabled=['imcontrol']))

    seen = []
    realReplace = os.replace

    def replace(source, target):
        # at the moment of the swap the new file is complete and the old one untouched
        seen.append((json.loads(open(source).read()), os.path.exists(target)))
        realReplace(source, target)

    monkeypatch.setattr(tools.os, 'replace', replace)
    tools._writeModulesFile()
    assert json.loads(path.read_text()) == {'enabled': ['imcontrol']}
    assert seen == [({'enabled': ['imcontrol']}, False)]
    assert os.listdir(tmp_path) == ['modules.json']            # no temporary file left

    tools._writeModulesFile()                                    # same content: not rewritten
    assert len(seen) == 1

    tools.setEnabledModuleIds(['imcontrol', 'improcess'])
    assert json.loads(path.read_text()) == {'enabled': ['imcontrol', 'improcess']}
    assert len(seen) == 2 and seen[1][1] is True
