"""The Fiji controller: menu state, send/import flows, failures, and one real-thread run.

The unit tests use a fake session object and an inline job runner, so no
process or thread is involved. One integration test runs the real worker
thread against a real :class:`FijiSession` talking to the protocol-speaking
fake host (no Java).
"""

from __future__ import annotations

import os
import platform
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtCore, QtWidgets  # noqa: E402

from imswitch.improcess.controller import FijiController as controller_module  # noqa: E402
from imswitch.improcess.controller.FijiController import FijiController, resolve_source  # noqa: E402
from imswitch.improcess.fiji.convert import PROP_RESULT_UID, SentRecord  # noqa: E402
from imswitch.improcess.fiji.preferences import (  # noqa: E402
    LOCATION_HINT,
    FijiPreferences,
    load_fiji_preferences,
    save_fiji_preferences,
)
from imswitch.improcess.fiji.protocol import (  # noqa: E402
    FijiImageDescription,
    HostError,
    PingInfo,
)
from imswitch.improcess.fiji.session import FijiDied, FijiSession, FijiStartError  # noqa: E402
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.napari_import import NotImportable  # noqa: E402
from imswitch.improcess.model.provenance import output_node  # noqa: E402

FAKE = "imswitch.improcess._test.fiji_fake_host"


# -- stubs ----------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self._slots = []
        self.emitted = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        self.emitted.append(args)
        for slot in list(self._slots):
            slot(*args)


class _View:
    def __init__(self):
        self.sigClosing = _Signal()
        self.menu = QtWidgets.QMenu()
        self.statuses = []

    def fijiMenu(self):
        return self.menu

    def showStatusMessage(self, message, timeout_ms=0):
        self.statuses.append(message)


class _Comm:
    def __init__(self):
        self.sigResultProduced = _Signal()
        self.results = []

    def getAllResults(self):
        return list(self.results)


class _Reconstruction:
    def __init__(self):
        self.active = None

    def getActiveResult(self):
        return self.active


def _inline(tag, job, on_done, on_failed):
    try:
        value = job()
    except Exception as exc:  # noqa: BLE001
        on_failed(exc)
    else:
        on_done(value)


def _desc(image_id, *, uid="", title="img", shape=(1, 1, 1, 4, 5), bit_depth=16):
    t, z, c, y, x = shape
    return FijiImageDescription(
        image_id=image_id, title=title, dims_xyczt=(x, y, c, z, t), bit_depth=bit_depth,
        pixel_width=1.0, pixel_height=1.0, pixel_depth=1.0,
        x_unit="pixel", y_unit="pixel", z_unit="pixel", x_origin=0.0, y_origin=0.0, z_origin=0.0,
        frame_interval=0.0, time_unit="sec",
        properties={PROP_RESULT_UID: uid} if uid else {}, channel_labels=(), changes=False,
    )


class FakeSession:
    instances: list = []

    def __init__(self, config, python=None):
        self.config, self.python = config, python
        self.state = "stopped"
        self.sent: dict = {}
        self.ping_info = None
        self.images: dict = {}
        self.fetched: list = []
        self.fail_start = None
        self.fail_fetch = None
        self._next = -1
        FakeSession.instances.append(self)

    def start(self):
        if self.fail_start:
            self.state = "failed"
            raise self.fail_start
        self.state = "ready"
        self.ping_info = PingInfo(1, "2.18.0", "1.54p", "21.0.7", False, 1, 1000)

    def stop(self, timeout_s=None):
        self.state, self.sent, self.ping_info = "stopped", {}, None

    def send(self, plan):
        image_id, self._next = self._next, self._next - 1
        uid = plan.properties.get(PROP_RESULT_UID, "")
        self.sent[image_id] = SentRecord(image_id, uid, tuple(plan.source_axes), tuple(plan.data.shape))
        self.images[image_id] = (_desc(image_id, uid=uid, title=plan.title), np.asarray(plan.data))
        return image_id

    def list_images(self):
        return [d for d, _ in self.images.values()]

    def fetch(self, image_id):
        if self.fail_fetch is not None:
            if isinstance(self.fail_fetch, FijiDied):
                self.state = "dead"
            raise self.fail_fetch
        self.fetched.append(image_id)
        desc, data = self.images[image_id]
        return data, desc

    def add_foreign(self, uid=""):
        image_id, self._next = self._next, self._next - 1
        data = np.arange(20, dtype=np.uint16).reshape(1, 1, 1, 4, 5)
        self.images[image_id] = (_desc(image_id, uid=uid, title="foreign"), data)
        return image_id


def _result(name="src", uid=None):
    data = np.arange(20, dtype=np.uint16).reshape(4, 5)
    result = ArrayProcessingResult(name, data, ["Y", "X"], axis_scales=[0.2, 0.1], scale_unit="um")
    return result


def _fiji_tree(tmp_path):
    root = tmp_path / "Fiji"
    home = root / "java" / "macos-arm64" / "zulu21" / "zulu-21.jdk" / "Contents" / "Home"
    (root / "jars").mkdir(parents=True)
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    (root / "config" / "jaunch").mkdir(parents=True)
    (root / "config" / "jaunch" / "fiji.toml").write_text("cfg.max-heap = '75%'\n")
    return root


@pytest.fixture(autouse=True)
def _apple_silicon(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    FakeSession.instances = []


@pytest.fixture
def warnings(monkeypatch):
    seen = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: seen.append(a[1:]))
    return seen


class Env:
    def __init__(self, tmp_path, *, configured=True, runner=_inline, factory=FakeSession):
        self.tmp_path = tmp_path
        self.prefs_path = str(tmp_path / "improcess_options.json")
        self.root = _fiji_tree(tmp_path)
        if configured:
            save_fiji_preferences(FijiPreferences(fijiDir=str(self.root)), self.prefs_path)
        self.view, self.comm, self.recon = _View(), _Comm(), _Reconstruction()
        self.controller = FijiController(
            self.comm, self.view, self.recon, preferences_path=self.prefs_path,
            session_factory=factory, job_runner=runner, find_spec=lambda name: object(),
        )

    def actions(self):
        self.controller.rebuildMenu()
        return {a.text(): a for a in self.view.menu.actions() if a.text()}

    @property
    def session(self):
        return FakeSession.instances[-1]

    @property
    def status(self):
        return self.view.statuses[-1]


@pytest.fixture
def env(tmp_path, qapp):
    return Env(tmp_path)


# -- menu -----------------------------------------------------------------------

def test_menu_when_fiji_is_not_configured(tmp_path, qapp):
    env = Env(tmp_path, configured=False)
    env.recon.active = _result()
    actions = env.actions()
    assert actions["Fiji: not running"].isEnabled() is False
    assert not actions["Start Fiji"].isEnabled() and actions["Start Fiji"].toolTip() == LOCATION_HINT
    assert not actions["Send to Fiji"].isEnabled()
    assert actions["Fiji location…"].isEnabled()


def test_menu_states_follow_the_session(env):
    actions = env.actions()
    assert actions["Start Fiji"].isEnabled() and not actions["Send to Fiji"].isEnabled()
    assert "Select a result" in actions["Send to Fiji"].toolTip()
    assert not actions["Import from Fiji…"].isEnabled() and not actions["Stop Fiji"].isEnabled()

    env.recon.active = _result()
    assert env.actions()["Send to Fiji"].isEnabled()

    env.controller.startFiji()
    actions = env.actions()
    assert "Fiji: ImageJ 2.18.0 (Java 21.0.7)" in actions
    assert not actions["Start Fiji"].isEnabled()
    assert actions["Import from Fiji…"].isEnabled() and actions["Import new images"].isEnabled()
    assert actions["Stop Fiji"].isEnabled()
    assert all(a.toolTip() for a in actions.values() if a.isEnabled() is False and a.text() != "Fiji: ImageJ 2.18.0 (Java 21.0.7)")


def test_missing_pyimagej_disables_everything_with_the_install_hint(tmp_path, qapp):
    env = Env(tmp_path)
    env.controller._findSpec = lambda name: None
    env.recon.active = _result()
    actions = env.actions()
    assert 'imswitch2[fiji]' in actions["Start Fiji"].toolTip()
    assert not actions["Start Fiji"].isEnabled() and not actions["Send to Fiji"].isEnabled()


def test_stop_returns_to_not_running(env):
    env.controller.startFiji()
    env.controller.stopFiji()
    assert env.session.state == "stopped" and "Fiji: not running" in env.actions()
    assert env.status == "Fiji stopped."


# -- sending --------------------------------------------------------------------

def test_a_result_that_cannot_be_sent_starts_nothing(env):
    bad = ArrayProcessingResult("angles", np.zeros((2, 4, 5), np.uint16), ["Angle", "Y", "X"])
    env.recon.active = bad
    env.controller.sendCurrent()
    assert "cannot be sent to Fiji" in env.status
    assert FakeSession.instances == []


def test_send_starts_fiji_then_sends_and_remembers_the_result(env):
    result = _result()
    env.recon.active = result
    env.controller.sendCurrent()
    session = env.session
    assert session.state == "ready" and len(session.sent) == 1
    (image_id,) = session.sent
    assert env.controller._sentResults[image_id] is result
    assert env.status == f"Sent src to Fiji (image {image_id})."
    assert session.python == sys.executable and session.config.fiji_dir == env.root


def test_a_second_send_reuses_the_running_session(env):
    env.recon.active = _result()
    env.controller.sendCurrent()
    env.controller.sendCurrent()
    assert len(FakeSession.instances) == 1 and len(env.session.sent) == 2


def test_fiji_python_preference_reaches_the_session(env):
    save_fiji_preferences(FijiPreferences(str(env.root), "/envs/fiji/bin/python"), env.prefs_path)
    env.controller.startFiji()
    assert env.session.python == "/envs/fiji/bin/python"


# -- importing ------------------------------------------------------------------

def test_import_new_picks_only_unseen_unsent_images(env):
    env.recon.active = _result()
    env.controller.sendCurrent()
    session = env.session
    seen = session.add_foreign()
    env.controller.importNewImages()                      # imports `seen`
    assert session.fetched == [seen]
    new = session.add_foreign()
    env.controller.importNewImages()
    assert session.fetched == [seen, new]                 # the sent one and `seen` were not fetched again
    env.controller.importNewImages()
    assert env.status == "No new images in Fiji."


def test_an_import_emits_a_result_with_external_import_provenance(env):
    env.controller.startFiji()
    image_id = env.session.add_foreign()
    env.controller.importNewImages()
    ((result, name),) = env.comm.sigResultProduced.emitted
    assert name == result.name and image_id in env.controller._imported
    node = output_node(result)
    assert node["op"] == "external-import" and node["tool"] == "fiji"
    assert node["details"]["imagej"] == "2.18.0" and node["details"]["java"] == "21.0.7"
    assert "metadata notes; see Metadata" in env.status


def test_the_uid_hint_names_the_source(env):
    source = _result("origin")
    env.comm.results = [("origin", source)]
    env.controller.startFiji()
    env.session.add_foreign(uid=source.result_uid)
    env.controller.importNewImages()
    ((result, _),) = env.comm.sigResultProduced.emitted
    assert output_node(result)["inputs"]


def test_source_resolution_order():
    sent_result, hinted = object(), object()
    desc = _desc(-3, uid="u1")
    assert resolve_source(-3, desc, {-3}, {-3: sent_result}, {"u1": hinted}) is sent_result
    assert resolve_source(-3, desc, set(), {}, {"u1": hinted}) is hinted
    assert resolve_source(-3, _desc(-3), set(), {}, {"u1": hinted}) is None
    assert resolve_source(-3, _desc(-3, uid="other"), set(), {}, {"u1": hinted}) is None


def test_one_message_per_image_that_cannot_be_imported(env):
    env.controller.startFiji()
    a, b = env.session.add_foreign(), env.session.add_foreign()
    env.session.fail_fetch = HostError("RGB images are not imported")
    before = len(env.view.statuses)
    env.controller.importNewImages()
    messages = [m for m in env.view.statuses[before:] if "Could not import" in m]
    assert len(messages) == 2 and f"{a}" in messages[0] and f"{b}" in messages[1]
    env.session.fail_fetch = NotImportable("no honest form")
    env.controller.importNewImages()
    assert env.comm.sigResultProduced.emitted == []


# -- the chooser ----------------------------------------------------------------

class _StubDialog:
    """Stands in for the modal chooser: records what it was built with, answers a canned choice."""

    built: list = []
    choice: list = []
    accept = True

    def __init__(self, descriptions, sent=None, imported=None, source_names=None, parent=None):
        self.args = (list(descriptions), dict(sent), set(imported), dict(source_names))
        self.sigRefreshRequested = _Signal()
        self.refreshed = []
        _StubDialog.built.append(self)

    def exec_(self):
        return QtWidgets.QDialog.Accepted if _StubDialog.accept else QtWidgets.QDialog.Rejected

    def selectedIds(self):
        return list(_StubDialog.choice)

    def setDescriptions(self, descriptions, **kwargs):
        self.refreshed.append((list(descriptions), kwargs))


def test_import_from_fiji_imports_the_chosen_images_through_the_dialog(env):
    _StubDialog.built, _StubDialog.accept = [], True
    env.controller._dialogFactory = _StubDialog
    result = _result("origin")
    env.comm.results = [("origin", result)]
    env.recon.active = result
    env.controller.sendCurrent()
    session = env.session
    a, b = session.add_foreign(uid=result.result_uid), session.add_foreign()
    _StubDialog.choice = [b]
    env.controller.importFromFiji()
    (dialog,) = _StubDialog.built
    descriptions, sent, imported, names = dialog.args
    assert [d.image_id for d in descriptions] == list(session.images)
    assert names[a] == "origin" and names[b] is None and set(sent) == set(env.controller._sentResults)
    assert session.fetched == [b] and b in env.controller._imported

    dialog.sigRefreshRequested.emit()                       # Refresh re-lists and updates the same dialog
    assert len(dialog.refreshed) == 1 and len(dialog.refreshed[0][0]) == len(session.images)


def test_a_cancelled_chooser_imports_nothing(env):
    _StubDialog.built, _StubDialog.accept, _StubDialog.choice = [], False, [-9]
    env.controller._dialogFactory = _StubDialog
    env.controller.startFiji()
    env.session.add_foreign()
    env.controller.importFromFiji()
    assert env.session.fetched == [] and env.comm.sigResultProduced.emitted == []


# -- failures -------------------------------------------------------------------

def test_fiji_dying_clears_the_sent_map_and_says_so(env):
    env.recon.active = _result()
    env.controller.sendCurrent()
    assert env.controller._sentResults
    env.session.add_foreign()
    env.session.fail_fetch = FijiDied("the Fiji host process died")
    env.controller.importNewImages()
    assert env.controller._sentResults == {} and env.controller._imported == set()
    assert env.status == "Fiji stopped unexpectedly."
    actions = env.actions()
    assert "Fiji: stopped unexpectedly" in actions and actions["Start Fiji"].isEnabled()
    first = env.session
    env.controller.startFiji()
    assert env.session is not first and env.session.state == "ready"      # a fresh host


def test_a_start_error_shows_a_warning_with_the_message(env, warnings):
    class Failing(FakeSession):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail_start = FijiStartError("exited with code 3\n--- host log ---\nboom")

    env.controller._sessionFactory = Failing
    env.controller.startFiji()
    assert warnings and "boom" in warnings[0][1]
    assert "Fiji: failed to start" in env.actions()
    assert env.actions()["Start Fiji"].isEnabled()


def test_a_start_error_during_send_warns_once(env, warnings):
    class Failing(FakeSession):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail_start = FijiStartError("no java")

    env.controller._sessionFactory = Failing
    env.recon.active = _result()
    env.controller.sendCurrent()
    assert len(warnings) == 1 and env.controller._sentResults == {}


# -- location -------------------------------------------------------------------

def test_location_is_validated_and_saved(env, monkeypatch, warnings, tmp_path):
    other = _fiji_tree(tmp_path / "second")
    (other / "Fiji.app").mkdir()
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(other / "Fiji.app"))
    env.controller.chooseLocation()
    assert load_fiji_preferences(env.prefs_path).fijiDir == str(other) and not warnings

    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    env.controller.chooseLocation()
    assert warnings and "jars" in warnings[0][1]
    assert load_fiji_preferences(env.prefs_path).fijiDir == str(other)       # the old value stays

    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    env.controller.chooseLocation()                                           # cancelled: nothing changes
    assert load_fiji_preferences(env.prefs_path).fijiDir == str(other)


def test_changing_the_location_while_running_says_when_it_applies(env, monkeypatch, tmp_path):
    env.controller.startFiji()
    other = _fiji_tree(tmp_path / "second")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory", lambda *a, **k: str(other))
    env.controller.chooseLocation()
    assert "next time Fiji is started" in env.status


# -- closing --------------------------------------------------------------------

def test_closing_stops_the_session_inline(env):
    env.controller.startFiji()
    env.view.sigClosing.emit()
    assert env.session.state == "stopped"


# -- the real worker thread -----------------------------------------------------

def test_real_thread_and_real_session_on_the_fake_host(tmp_path, qtbot):
    made = []

    def factory(config, python=None):
        session = FijiSession(config, python=python, host_module=FAKE, temp_root=tmp_path,
                              startup_timeout_s=60.0)
        made.append(session)
        return session

    env = Env(tmp_path, runner=None, factory=factory)
    controller = env.controller
    result = _result()
    env.recon.active = result
    env.comm.results = [("src", result)]
    try:
        controller.sendCurrent()
        qtbot.waitUntil(lambda: bool(controller._sentResults), timeout=60000)
        session = made[0]
        assert controller.state() == "ready" and controller._thread.isRunning()
        (image_id,) = session.sent
        assert controller._sentResults[image_id] is result

        dup = session.run_command(image_id, "Duplicate...", "title=dup")
        controller.importNewImages()
        qtbot.waitUntil(lambda: bool(env.comm.sigResultProduced.emitted), timeout=60000)
        ((imported, _),) = env.comm.sigResultProduced.emitted
        assert dup in controller._imported
        node = output_node(imported)
        assert node["tool"] == "fiji" and node["inputs"]        # the lineage hint found the sent result

        thread, process = controller._thread, session.process
        controller.close()
        assert not thread.isRunning() and process.poll() is not None
        assert session.state == "stopped" and controller._thread is None
    finally:
        for session in made:
            session.stop(timeout_s=1.0)
        if controller._thread is not None:
            controller.close()



def test_the_close_margin_is_a_named_bound():
    assert controller_module.CLOSE_MARGIN_S > 0
    assert QtCore.QThread is not None
