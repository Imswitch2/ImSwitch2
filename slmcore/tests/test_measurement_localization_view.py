import pytest


def _localization_defaults():
    try:
        from slmcore.cgh.localization import LOCALIZATION_PARAMS
    except Exception as error:
        pytest.skip(f"Localization parameter dependencies are unavailable: {error}")
    return {
        key:spec.default
        for key,spec in LOCALIZATION_PARAMS.items()
    }


def _qapp_and_view_class():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.measurement.localization_view import (
            MeasurementLocalizationView,
        )
    except Exception as error:
        pytest.skip(f"MeasurementLocalizationView dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app,QtWidgets,MeasurementLocalizationView


def _button_with_text(widget,QtWidgets,text):
    for button in widget.findChildren(QtWidgets.QPushButton):
        if button.text() == text:
            return button
    return None


def _label_texts(widget,QtWidgets):
    return [
        label.text()
        for label in widget.findChildren(QtWidgets.QLabel)
    ]


def test_measurement_localization_view_can_show_metrics_and_accept():
    _app,QtWidgets,MeasurementLocalizationView = _qapp_and_view_class()
    view = MeasurementLocalizationView(
        parameters=_localization_defaults(),
        detectors=("Camera",),
        current_detector="Camera",
        show_metrics=True,
        show_accept=True,
    )
    try:
        labels = _label_texts(view,QtWidgets)
        assert "Measurement Metrics" in labels
        assert "Uniformity" in labels
        assert "Efficiency" in labels
        assert _button_with_text(view,QtWidgets,"Accept Localization") is not None
    finally:
        view.deleteLater()


def test_measurement_localization_view_can_hide_metrics_and_accept():
    _app,QtWidgets,MeasurementLocalizationView = _qapp_and_view_class()
    view = MeasurementLocalizationView(
        parameters=_localization_defaults(),
        show_metrics=False,
        show_accept=False,
    )
    try:
        labels = _label_texts(view,QtWidgets)
        assert "Measurement Metrics" not in labels
        assert "Uniformity" not in labels
        assert "Efficiency" not in labels
        assert _button_with_text(view,QtWidgets,"Accept Localization") is None
    finally:
        view.deleteLater()


def test_measurement_localization_view_forwards_generic_actions():
    _app,QtWidgets,MeasurementLocalizationView = _qapp_and_view_class()
    view = MeasurementLocalizationView(
        parameters=_localization_defaults(),
        detectors=("Camera",),
        current_detector="Camera",
        show_accept=True,
    )
    acquired = []
    accepted = []
    view.sigAcquireRequested.connect(acquired.append)
    view.sigAcceptRequested.connect(lambda:accepted.append(True))

    try:
        acquire = _button_with_text(view,QtWidgets,"Acquire")
        accept = _button_with_text(view,QtWidgets,"Accept Localization")
        assert acquire is not None
        assert accept is not None

        acquire.click()
        accept.click()

        assert acquired == ["Camera"]
        assert accepted == [True]
    finally:
        view.deleteLater()


def test_measurement_localization_view_read_only_disables_accept_action():
    _app,QtWidgets,MeasurementLocalizationView = _qapp_and_view_class()
    view = MeasurementLocalizationView(
        parameters=_localization_defaults(),
        show_accept=True,
    )
    try:
        accept = _button_with_text(view,QtWidgets,"Accept Localization")
        assert accept is not None

        view.set_accept_enabled(True)
        assert accept.isEnabled()

        view.set_read_only(True)
        assert not accept.isEnabled()

        view.set_read_only(False)
        assert accept.isEnabled()
    finally:
        view.deleteLater()
