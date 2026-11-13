"""File > New script from current settings: an unsaved tab holding the
generated script; a plain warning when ImControl is not loaded."""
from types import SimpleNamespace

import pytest

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imscripting.controller.basecontrollers import ImScrWidgetControllerFactory
from imswitch.imscripting.controller.CommunicationChannel import CommunicationChannel
from imswitch.imscripting.controller.ImScrMainViewController import ImScrMainViewController
from imswitch.imscripting.model import getActionsScope
from imswitch.imscripting.view.ImScrMainView import ImScrMainView

from .test_acquisition_script import _Imcontrol


def _window(qtbot, api):
    channel = ModuleCommunicationChannel()
    scope = {'moduleCommChannel': channel}
    if api is not None:
        scope['api'] = api
    scope.update(getActionsScope(scope.copy()))
    view = ImScrMainView()
    qtbot.addWidget(view)
    factory = ImScrWidgetControllerFactory(scope, CommunicationChannel(), channel)
    controller = factory.createController(ImScrMainViewController, view)
    return view, controller


def test_the_menu_opens_the_copied_acquisition_unsaved(qtbot):
    view, controller = _window(qtbot, SimpleNamespace(imcontrol=_Imcontrol()))
    tabsBefore = view.editor.count()

    view.newFromSettingsAction.trigger()

    assert view.editor.count() == tabsBefore + 1
    instance = view.editor.getCurrentInstance()
    assert instance.getText().startswith('"""Acquisition copied from ImSwitch')
    entry = controller.editorController.scriptStore[instance.getID()]
    assert entry.unsaved is True and entry.filePath is None    # closing asks first
    controller.editorController.scriptExecutor.shutdown(timeoutMs=2000)


def test_without_imcontrol_it_says_so(qtbot, monkeypatch):
    warned = []
    monkeypatch.setattr('imswitch.imscripting.view.guitools.showWarning',
                        lambda _w, _t, message, **_k: warned.append(message))
    view, controller = _window(qtbot, SimpleNamespace())
    tabsBefore = view.editor.count()

    assert controller.editorController.newFromCurrentSettings() is None

    assert view.editor.count() == tabsBefore
    assert warned and 'ImControl is not loaded' in warned[0]
    controller.editorController.scriptExecutor.shutdown(timeoutMs=2000)
