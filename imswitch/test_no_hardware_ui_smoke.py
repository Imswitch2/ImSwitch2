from pathlib import Path

import pytest


pytestmark = [pytest.mark.nohardware, pytest.mark.ui]


PROFILE_PATH = (
    Path(__file__).resolve().parent
    / "_data"
    / "user_defaults"
    / "imcontrol_setups"
    / "example_no_hardware.json"
)


def test_no_hardware_profile_constructs_imcontrol_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcommon.model import dirtools
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo

    assert Path(dirtools.UserFileDirs.Root).is_relative_to(tmp_path / "home")

    view_setup_info = ViewSetupInfo.from_json(PROFILE_PATH.read_text(), infer_missing=True)
    options = Options(setupFileName=PROFILE_PATH.name)
    module_comm_channel = ModuleCommunicationChannel()
    module_comm_channel.register(imcontrol)

    app = prepareApp()
    view = None
    controller = None

    try:
        view, controller = imcontrol.getMainViewAndController(
            module_comm_channel,
            overrideSetupInfo=view_setup_info,
            overrideOptions=options,
        )
        app.processEvents()

        expected_widgets = {
            "Settings",
            "View",
            "Recording",
            "Image",
            "Positioner",
            "ViewerTools",
            "LineProfile",
        }
        assert expected_widgets.issubset(view.widgets.keys())
        assert expected_widgets.issubset(controller.controllers.keys())
    finally:
        if view is not None:
            view.close()
            app.processEvents()
