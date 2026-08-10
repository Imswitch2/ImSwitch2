#!/usr/bin/env python
"""Interactive AA Opto-Electronic AOTF frequency and power calibration.

The utility is intentionally standalone: ImSwitch must be closed so this
process can own the configured serial ports.  It reads an ImSwitch setup JSON,
discovers every ``AAAOTFLaserManager``, opens each distinct RS232 device once,
and provides three operations:

* manually test a frequency and raw power value;
* sweep frequency while measuring a Thorlabs PM100D and select the peak;
* sweep raw AOTF power and save the two-column calibration LUT used by
  ``AAAOTFLaserManager``.

Saving a selected device writes ``protocolProfile``, ``frequencyMHz``,
``valueRangeMin``, ``valueRangeMax`` and ``calibCsvPath`` back to the setup.
The original setup is copied to a timestamped ``.bak`` file and the edited JSON
is committed atomically.

Run from the repository root (in the ImSwitch environment):

    python utility_scripts/aa_aotf_calibration.py

or open a setup immediately:

    python utility_scripts/aa_aotf_calibration.py /path/to/setup.json

Safety: connecting does not send an enable command.  Test and sweep operations
are explicit, and the selected channel is switched off after every sweep,
after failures, and when the window closes.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Iterable

import numpy as np

try:
    from PyQt5 import QtCore, QtWidgets
except ImportError as exc:  # pragma: no cover - depends on the launch env
    raise SystemExit(
        "PyQt5 is required. Run this utility from the same environment as "
        "ImSwitch."
    ) from exc


FREQUENCY_PROFILE = "aa.frequency-startup"


@dataclass(frozen=True)
class AOTFDefinition:
    """One AA AOTF laser entry resolved from an ImSwitch setup."""

    name: str
    rs232_name: str
    channel: int
    wavelength_nm: float
    value_min: float
    value_max: float
    frequency_mhz: float | None
    calibration_path: str


@dataclass(frozen=True)
class SweepSpec:
    """Parameters passed to the background measurement worker."""

    kind: str
    device: AOTFDefinition
    values: tuple[float, ...]
    fixed_value: float
    settle_seconds: float


def _as_float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be numeric, got {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return number


def format_frequency_mhz(value: Any) -> str:
    """Format an AA frequency while retaining ``.0`` for whole MHz values."""
    number = _as_float(value, field="Frequency")
    if number <= 0:
        raise ValueError(f"Frequency must be positive, got {value!r}")
    formatted = format(number, ".12g")
    if "e" not in formatted.lower() and "." not in formatted:
        formatted += ".0"
    return formatted


def format_drive(value: Any) -> str:
    """Format a non-negative raw AA power/amplitude value."""
    number = _as_float(value, field="Power")
    if number < 0:
        raise ValueError(f"Power must not be negative, got {value!r}")
    rounded = round(number)
    if math.isclose(number, rounded, rel_tol=0, abs_tol=1e-9):
        return str(int(rounded))
    return format(number, ".12g")


def build_output_command(
    channel: int,
    frequency_mhz: Any,
    drive: Any,
    *,
    enabled: bool,
) -> str:
    """Build the field-verified combined AA channel command."""
    channel = int(channel)
    if channel < 1:
        raise ValueError(f"AA channels are 1-based, got {channel}")
    return (
        f"L{channel}F{format_frequency_mhz(frequency_mhz)}"
        f"P{format_drive(drive)}O{1 if enabled else 0}"
    )


def load_setup(path: Path) -> dict[str, Any]:
    """Load and minimally validate an ImSwitch setup JSON file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("The setup root must be a JSON object")
    if not isinstance(data.get("lasers", {}), dict):
        raise ValueError("The setup 'lasers' section must be an object")
    if not isinstance(data.get("rs232devices", {}), dict):
        raise ValueError("The setup 'rs232devices' section must be an object")
    return data


def discover_aotfs(setup: dict[str, Any]) -> list[AOTFDefinition]:
    """Return all configured ``AAAOTFLaserManager`` devices.

    Each laser is checked against its referenced RS232 entry here, before any
    hardware connection is attempted, so configuration mistakes remain
    distinguishable from connection failures.
    """
    rs232_devices = setup.get("rs232devices", {})
    found: list[AOTFDefinition] = []
    for name, laser in setup.get("lasers", {}).items():
        if not isinstance(laser, dict):
            continue
        if laser.get("managerName") != "AAAOTFLaserManager":
            continue
        properties = laser.get("managerProperties") or {}
        if not isinstance(properties, dict):
            raise ValueError(f"Laser {name!r} has invalid managerProperties")
        rs232_name = str(properties.get("rs232device", "")).strip()
        if not rs232_name:
            raise ValueError(f"AA AOTF {name!r} has no rs232device")
        if rs232_name not in rs232_devices:
            raise ValueError(
                f"AA AOTF {name!r} references missing RS232 device "
                f"{rs232_name!r}"
            )
        try:
            channel = int(properties["channel"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"AA AOTF {name!r} must have an integer channel"
            ) from None
        if channel < 1:
            raise ValueError(
                f"AA AOTF {name!r} channel must be at least 1"
            )

        configured_frequency = properties.get("frequencyMHz")
        frequency = None
        if configured_frequency not in (None, "", 0, 0.0, "0", "0.0"):
            frequency = _as_float(
                configured_frequency, field=f"{name} frequencyMHz"
            )
            if frequency <= 0:
                raise ValueError(
                    f"AA AOTF {name!r} frequencyMHz must be positive"
                )

        found.append(AOTFDefinition(
            name=str(name),
            rs232_name=rs232_name,
            channel=channel,
            wavelength_nm=_as_float(
                laser.get("wavelength", 0), field=f"{name} wavelength"
            ),
            value_min=_as_float(
                laser.get("valueRangeMin", 0), field=f"{name} valueRangeMin"
            ),
            value_max=_as_float(
                laser.get("valueRangeMax", 1023),
                field=f"{name} valueRangeMax",
            ),
            frequency_mhz=frequency,
            calibration_path=str(properties.get("calibCsvPath") or ""),
        ))
    return found


def apply_calibration_to_setup(
    setup: dict[str, Any],
    *,
    laser_name: str,
    frequency_mhz: float,
    value_min: float,
    value_max: float,
    calibration_path: Path,
) -> None:
    """Update one laser entry with its selected calibration settings."""
    frequency = _as_float(frequency_mhz, field="frequencyMHz")
    minimum = _as_float(value_min, field="valueRangeMin")
    maximum = _as_float(value_max, field="valueRangeMax")
    if frequency <= 0:
        raise ValueError("frequencyMHz must be positive")
    if minimum < 0:
        raise ValueError("valueRangeMin must not be negative")
    if maximum <= minimum:
        raise ValueError("valueRangeMax must be greater than valueRangeMin")
    calibration_path = Path(calibration_path).expanduser().resolve()

    try:
        laser = setup["lasers"][laser_name]
    except KeyError:
        raise ValueError(f"Laser {laser_name!r} is not in the setup") from None
    if laser.get("managerName") != "AAAOTFLaserManager":
        raise ValueError(f"Laser {laser_name!r} is not an AA AOTF")

    properties = laser.setdefault("managerProperties", {})
    properties["protocolProfile"] = FREQUENCY_PROFILE
    properties["frequencyMHz"] = frequency
    properties["calibCsvPath"] = str(calibration_path)
    laser["valueRangeMin"] = minimum
    laser["valueRangeMax"] = maximum


def _atomic_text_write(path: Path, text: str) -> None:
    """Write text through a temporary file in the destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def save_setup_with_backup(path: Path, setup: dict[str, Any]) -> Path:
    """Back up and atomically overwrite a setup JSON; return backup path."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Setup file does not exist: {path}")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.{timestamp}.bak")
    shutil.copy2(path, backup)
    try:
        serialized = json.dumps(setup, indent=2, ensure_ascii=False) + "\n"
        _atomic_text_write(path, serialized)
    except Exception:
        # The original setup is still in place if temp-write failed.  Keep the
        # backup as evidence/recovery for any failure after it was created.
        raise
    return backup


def save_calibration_csv(
    path: Path,
    rows: Iterable[tuple[float, float]],
    *,
    laser_name: str,
    wavelength_nm: float,
    frequency_mhz: float,
    notes: str = "",
) -> None:
    """Save the raw-drive/measured-mW LUT atomically."""
    data = np.asarray(list(rows), dtype=float)
    if data.ndim != 2 or data.shape[0] < 2 or data.shape[1] != 2:
        raise ValueError("Calibration requires at least two power measurements")
    if not np.all(np.isfinite(data)):
        raise ValueError("Calibration contains non-finite values")

    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as fh:
            header = (
                "Raw AOTF power, measured optical power [mW] | "
                f"laser={laser_name}, wavelength={wavelength_nm:g} nm, "
                f"frequency={frequency_mhz:g} MHz"
            )
            footer = notes.strip().replace("\n", " | ")
            np.savetxt(
                fh,
                data,
                delimiter=" ",
                header=header,
                footer=footer,
                comments="# ",
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


class AOTFTransportPool:
    """One direct, non-mocking serial transport per referenced RS232 device."""

    def __init__(self):
        self._transports: dict[str, Any] = {}

    @property
    def connected_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    def connect_all(
        self,
        definitions: Iterable[AOTFDefinition],
        setup: dict[str, Any],
    ) -> None:
        self.close()
        rs232_names = sorted({device.rs232_name for device in definitions})
        if not rs232_names:
            raise ValueError("No AA AOTFs were found in the setup")
        try:
            from imswitch.imcontrol.model.interfaces.RS232Driver import (
                generateDriverClass,
            )

            for name in rs232_names:
                entry = setup["rs232devices"][name]
                settings = entry.get("managerProperties") or {}
                port = settings.get("port")
                if not port:
                    raise ValueError(f"RS232 device {name!r} has no port")
                driver_class = generateDriverClass(settings)
                transport = driver_class(port)
                transport.initialize()
                self._transports[name] = transport
        except Exception:
            self.close()
            raise

    def require(self, device: AOTFDefinition):
        try:
            return self._transports[device.rs232_name]
        except KeyError:
            raise RuntimeError(
                f"RS232 device {device.rs232_name!r} is not connected"
            ) from None

    def prepare(self, device: AOTFDefinition) -> None:
        # This controller-global preamble is intentionally write-only; the
        # affected AA controller does not provide a reply to wait for.
        self.require(device).write("I0")

    def set_output(
        self,
        device: AOTFDefinition,
        *,
        frequency_mhz: float,
        drive: float,
        enabled: bool,
    ) -> str:
        command = build_output_command(
            device.channel,
            frequency_mhz,
            drive,
            enabled=enabled,
        )
        return self.require(device).query(command)

    def disable(self, device: AOTFDefinition) -> None:
        if device.rs232_name in self._transports:
            self._transports[device.rs232_name].query(
                f"L{device.channel}O0"
            )

    def disable_all(self, definitions: Iterable[AOTFDefinition]) -> list[str]:
        errors = []
        for device in definitions:
            try:
                self.disable(device)
            except Exception as exc:
                errors.append(f"{device.name}: {exc}")
        return errors

    def close(self) -> None:
        for transport in self._transports.values():
            try:
                transport.close()
            except Exception:
                pass
        self._transports.clear()


class PM100D:
    """Minimal direct-SCPI wrapper based on the supplied calibration script."""

    def __init__(self, serial: str, backend: str = "", timeout_ms: int = 2000):
        self.serial = str(serial).strip()
        self.backend = backend
        self.timeout_ms = int(timeout_ms)
        self._resource_manager = None
        self._instrument = None
        self.resource_name = None

    @property
    def connected(self) -> bool:
        return self._instrument is not None

    def connect(self) -> None:
        if self.connected:
            return
        if not self.serial:
            raise ValueError("Enter the PM100D serial number")
        import pyvisa

        self._resource_manager = pyvisa.ResourceManager(self.backend)
        resources = self._resource_manager.list_resources()
        self.resource_name = next(
            (resource for resource in resources if self.serial in resource),
            None,
        )
        if self.resource_name is None:
            self.close()
            raise RuntimeError(
                f"PM100D serial {self.serial!r} not found. VISA resources: "
                f"{resources}"
            )
        self._instrument = self._resource_manager.open_resource(
            self.resource_name, timeout=self.timeout_ms
        )

    def close(self) -> None:
        if self._instrument is not None:
            try:
                self._instrument.close()
            finally:
                self._instrument = None
        if self._resource_manager is not None:
            try:
                self._resource_manager.close()
            finally:
                self._resource_manager = None
        self.resource_name = None

    def _require(self):
        if self._instrument is None:
            raise RuntimeError("The PM100D is not connected")
        return self._instrument

    def identify(self) -> str:
        return self._require().query("*IDN?").strip()

    def read_power_mw(self) -> float:
        return float(self._require().query("READ?")) * 1e3

    def set_wavelength_nm(self, wavelength_nm: float) -> None:
        self._require().write(f"SENSE:CORR:WAV {float(wavelength_nm)}")

    def zero(self) -> None:
        self._require().write("SENSE:ZERO:INIT")


class SweepWorker(QtCore.QObject):
    """Run a frequency or power sweep without freezing the UI."""

    progress = QtCore.pyqtSignal(int, int, float, float)
    message = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str)

    def __init__(
        self,
        spec: SweepSpec,
        transports: AOTFTransportPool,
        power_meter: PM100D,
    ):
        super().__init__()
        self.spec = spec
        self.transports = transports
        self.power_meter = power_meter
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    @QtCore.pyqtSlot()
    def run(self) -> None:
        rows: list[tuple[float, float]] = []
        try:
            self.transports.prepare(self.spec.device)
            total = len(self.spec.values)
            for index, value in enumerate(self.spec.values, start=1):
                if self.cancelled:
                    self.message.emit("Sweep cancelled")
                    break
                if self.spec.kind == "frequency":
                    frequency, drive = value, self.spec.fixed_value
                else:
                    frequency, drive = self.spec.fixed_value, value
                self.transports.set_output(
                    self.spec.device,
                    frequency_mhz=frequency,
                    drive=drive,
                    enabled=True,
                )
                time.sleep(self.spec.settle_seconds)
                measured_mw = self.power_meter.read_power_mw()
                rows.append((value, measured_mw))
                self.progress.emit(index, total, value, measured_mw)
            self.finished.emit(self.spec.kind, rows)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.transports.disable(self.spec.device)
            except Exception as exc:
                self.message.emit(
                    f"WARNING: could not switch {self.spec.device.name} off: "
                    f"{exc}"
                )


class AOTFCalibrationWindow(QtWidgets.QMainWindow):
    """Config-driven AA AOTF calibration user interface."""

    def __init__(self, initial_setup: Path | None = None):
        super().__init__()
        self.setWindowTitle("AA AOTF frequency and power calibration")
        self.resize(940, 780)

        self.setup_path: Path | None = None
        self.setup: dict[str, Any] = {}
        self.devices: list[AOTFDefinition] = []
        self.transports = AOTFTransportPool()
        self.power_meter: PM100D | None = None
        self.frequency_results: dict[str, list[tuple[float, float]]] = {}
        self.power_results: dict[str, list[tuple[float, float]]] = {}
        self._thread: QtCore.QThread | None = None
        self._worker: SweepWorker | None = None

        self._build_ui()
        if initial_setup is not None:
            self.config_path_edit.setText(str(initial_setup))
            QtCore.QTimer.singleShot(0, self.load_selected_setup)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)

        setup_group = QtWidgets.QGroupBox("1. ImSwitch setup and connections")
        setup_layout = QtWidgets.QGridLayout(setup_group)
        self.config_path_edit = QtWidgets.QLineEdit()
        self.config_browse_button = QtWidgets.QPushButton("Browse…")
        self.config_load_button = QtWidgets.QPushButton("Load setup")
        self.device_combo = QtWidgets.QComboBox()
        self.connect_aotfs_button = QtWidgets.QPushButton("Connect all AOTFs")
        self.disconnect_aotfs_button = QtWidgets.QPushButton("Disconnect")
        self.aotf_status_label = QtWidgets.QLabel("Not connected")

        setup_layout.addWidget(QtWidgets.QLabel("Setup JSON"), 0, 0)
        setup_layout.addWidget(self.config_path_edit, 0, 1, 1, 4)
        setup_layout.addWidget(self.config_browse_button, 0, 5)
        setup_layout.addWidget(self.config_load_button, 0, 6)
        setup_layout.addWidget(QtWidgets.QLabel("AA AOTF"), 1, 0)
        setup_layout.addWidget(self.device_combo, 1, 1, 1, 2)
        setup_layout.addWidget(self.connect_aotfs_button, 1, 3)
        setup_layout.addWidget(self.disconnect_aotfs_button, 1, 4)
        setup_layout.addWidget(self.aotf_status_label, 1, 5, 1, 2)
        outer.addWidget(setup_group)

        meter_group = QtWidgets.QGroupBox("2. Thorlabs PM100D")
        meter_layout = QtWidgets.QGridLayout(meter_group)
        self.pm_serial_edit = QtWidgets.QLineEdit("P0011748")
        self.pm_backend_combo = QtWidgets.QComboBox()
        self.pm_backend_combo.addItem("Default VISA", "")
        self.pm_backend_combo.addItem("pyvisa-py (@py)", "@py")
        self.pm_connect_button = QtWidgets.QPushButton("Connect PM100D")
        self.pm_zero_button = QtWidgets.QPushButton("Zero")
        self.pm_read_button = QtWidgets.QPushButton("Read power")
        self.pm_status_label = QtWidgets.QLabel("Not connected")
        meter_layout.addWidget(QtWidgets.QLabel("Serial"), 0, 0)
        meter_layout.addWidget(self.pm_serial_edit, 0, 1)
        meter_layout.addWidget(self.pm_backend_combo, 0, 2)
        meter_layout.addWidget(self.pm_connect_button, 0, 3)
        meter_layout.addWidget(self.pm_zero_button, 0, 4)
        meter_layout.addWidget(self.pm_read_button, 0, 5)
        meter_layout.addWidget(self.pm_status_label, 0, 6)
        outer.addWidget(meter_group)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_manual_tab(), "Live test")
        self.tabs.addTab(self._build_frequency_tab(), "Find frequency")
        self.tabs.addTab(self._build_power_tab(), "Power calibration and save")
        outer.addWidget(self.tabs, 1)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button = QtWidgets.QPushButton("Cancel sweep")
        self.cancel_button.setEnabled(False)
        progress_row = QtWidgets.QHBoxLayout()
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.cancel_button)
        outer.addLayout(progress_row)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(5000)
        self.log_edit.setPlaceholderText("Connection and measurement log")
        outer.addWidget(self.log_edit, 1)

        self.config_browse_button.clicked.connect(self.browse_setup)
        self.config_load_button.clicked.connect(self.load_selected_setup)
        self.device_combo.currentIndexChanged.connect(self.device_changed)
        self.connect_aotfs_button.clicked.connect(self.connect_all_aotfs)
        self.disconnect_aotfs_button.clicked.connect(self.disconnect_aotfs)
        self.pm_connect_button.clicked.connect(self.connect_power_meter)
        self.pm_zero_button.clicked.connect(self.zero_power_meter)
        self.pm_read_button.clicked.connect(self.read_power_meter)
        self.cancel_button.clicked.connect(self.cancel_sweep)

    @staticmethod
    def _float_box(
        minimum: float,
        maximum: float,
        value: float,
        decimals: int = 3,
    ) -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setDecimals(decimals)
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setKeyboardTracking(False)
        return box

    def _build_manual_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)
        warning = QtWidgets.QLabel(
            "Output can emit laser light. Verify the beam path and begin with "
            "a low raw power."
        )
        warning.setStyleSheet("color: #b00020; font-weight: bold;")
        warning.setWordWrap(True)
        self.test_frequency_box = self._float_box(0.001, 10000, 143.0, 6)
        self.test_drive_box = self._float_box(0, 1e6, 7.0, 3)
        self.test_button = QtWidgets.QPushButton("Apply and enable selected channel")
        self.off_button = QtWidgets.QPushButton("Switch selected channel off")
        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(self.test_button)
        button_row.addWidget(self.off_button)
        form.addRow(warning)
        form.addRow("Frequency (MHz)", self.test_frequency_box)
        form.addRow("Raw AOTF power", self.test_drive_box)
        form.addRow(button_row)
        self.test_button.clicked.connect(self.test_selected_output)
        self.off_button.clicked.connect(self.disable_selected_output)
        return tab

    def _build_frequency_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)
        self.frequency_min_box = self._float_box(0.001, 10000, 138.0, 6)
        self.frequency_max_box = self._float_box(0.001, 10000, 148.0, 6)
        self.frequency_steps_box = QtWidgets.QSpinBox()
        self.frequency_steps_box.setRange(2, 10000)
        self.frequency_steps_box.setValue(101)
        self.frequency_drive_box = self._float_box(0, 1e6, 7.0, 3)
        self.frequency_settle_box = self._float_box(0, 60, 0.25, 3)
        self.frequency_sweep_button = QtWidgets.QPushButton(
            "Sweep and select peak frequency"
        )
        self.frequency_result_label = QtWidgets.QLabel("No sweep run")
        self.frequency_result_label.setWordWrap(True)
        form.addRow("Start frequency (MHz)", self.frequency_min_box)
        form.addRow("End frequency (MHz)", self.frequency_max_box)
        form.addRow("Number of points", self.frequency_steps_box)
        form.addRow("Raw power during sweep", self.frequency_drive_box)
        form.addRow("Settling time per point (s)", self.frequency_settle_box)
        form.addRow(self.frequency_sweep_button)
        form.addRow("Result", self.frequency_result_label)
        self.frequency_sweep_button.clicked.connect(self.run_frequency_sweep)
        return tab

    def _build_power_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(tab)
        self.save_frequency_box = self._float_box(0.001, 10000, 143.0, 6)
        self.power_min_box = self._float_box(0, 1e6, 0.0, 3)
        self.power_max_box = self._float_box(0, 1e6, 1023.0, 3)
        self.power_steps_box = QtWidgets.QSpinBox()
        self.power_steps_box.setRange(2, 10000)
        self.power_steps_box.setValue(150)
        self.power_settle_box = self._float_box(0, 60, 0.25, 3)
        self.power_sweep_button = QtWidgets.QPushButton("Run power calibration")
        self.calibration_path_edit = QtWidgets.QLineEdit()
        self.calibration_browse_button = QtWidgets.QPushButton("Choose…")
        calibration_row = QtWidgets.QHBoxLayout()
        calibration_row.addWidget(self.calibration_path_edit, 1)
        calibration_row.addWidget(self.calibration_browse_button)
        self.notes_edit = QtWidgets.QPlainTextEdit()
        self.notes_edit.setMaximumHeight(75)
        self.notes_edit.setPlaceholderText("Optional calibration notes")
        self.save_button = QtWidgets.QPushButton(
            "Save LUT and update selected laser in setup"
        )
        form.addRow("Selected frequency (MHz)", self.save_frequency_box)
        form.addRow("Minimum raw power", self.power_min_box)
        form.addRow("Maximum raw power", self.power_max_box)
        form.addRow("Number of points", self.power_steps_box)
        form.addRow("Settling time per point (s)", self.power_settle_box)
        form.addRow(self.power_sweep_button)
        form.addRow("Calibration CSV", calibration_row)
        form.addRow("Notes", self.notes_edit)
        form.addRow(self.save_button)
        self.power_sweep_button.clicked.connect(self.run_power_sweep)
        self.calibration_browse_button.clicked.connect(self.browse_calibration)
        self.save_button.clicked.connect(self.save_selected_calibration)
        return tab

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{stamp}] {message}")

    def show_error(self, title: str, exc: Exception | str) -> None:
        message = str(exc)
        self.log(f"ERROR: {message}")
        QtWidgets.QMessageBox.critical(self, title, message)

    def browse_setup(self) -> None:
        start = self.config_path_edit.text().strip()
        if not start:
            start = str(Path.home() / "ImSwitchConfig" / "imcontrol_setups")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open ImSwitch setup", start, "JSON setup (*.json)"
        )
        if path:
            self.config_path_edit.setText(path)

    def load_selected_setup(self) -> None:
        try:
            path = Path(self.config_path_edit.text().strip()).expanduser().resolve()
            setup = load_setup(path)
            devices = discover_aotfs(setup)
            if not devices:
                raise ValueError("No AAAOTFLaserManager entries found")
            self.disconnect_aotfs()
            self.setup_path = path
            self.setup = setup
            self.devices = devices
            self.device_combo.blockSignals(True)
            self.device_combo.clear()
            for device in devices:
                self.device_combo.addItem(
                    f"{device.name} — {device.rs232_name}, channel "
                    f"{device.channel}",
                    device.name,
                )
            self.device_combo.blockSignals(False)
            self.device_combo.setCurrentIndex(0)
            self.device_changed()
            self.log(
                f"Loaded {path}; found {len(devices)} AA AOTF channel(s) "
                f"on {len({d.rs232_name for d in devices})} serial connection(s)"
            )
        except Exception as exc:
            self.show_error("Could not load setup", exc)

    def selected_device(self) -> AOTFDefinition:
        name = self.device_combo.currentData()
        for device in self.devices:
            if device.name == name:
                return device
        raise RuntimeError("Select an AA AOTF")

    def device_changed(self) -> None:
        if not self.devices or self.device_combo.currentIndex() < 0:
            return
        device = self.selected_device()
        frequency = device.frequency_mhz or 143.0
        self.test_frequency_box.setValue(frequency)
        self.save_frequency_box.setValue(frequency)
        self.frequency_min_box.setValue(max(0.001, frequency - 5))
        self.frequency_max_box.setValue(frequency + 5)
        self.power_min_box.setValue(max(0, device.value_min))
        self.power_max_box.setValue(max(device.value_min + 1, device.value_max))
        safe_test = min(max(7.0, device.value_min), device.value_max)
        self.test_drive_box.setValue(safe_test)
        self.frequency_drive_box.setValue(safe_test)
        if device.calibration_path:
            self.calibration_path_edit.setText(device.calibration_path)
        elif self.setup_path is not None:
            calibration_dir = self.setup_path.parent.parent / "imcontrol_calibs"
            filename = f"{device.name.lower()}_calib.csv"
            self.calibration_path_edit.setText(str(calibration_dir / filename))
        self.frequency_result_label.setText(
            "Configured frequency: "
            + (f"{device.frequency_mhz:g} MHz" if device.frequency_mhz else "unset")
        )
        if self.power_meter is not None and self.power_meter.connected:
            try:
                self.power_meter.set_wavelength_nm(device.wavelength_nm)
                self.log(
                    f"PM100D wavelength set to {device.wavelength_nm:g} nm for "
                    f"{device.name}"
                )
            except Exception as exc:
                self.log(f"WARNING: could not set PM100D wavelength: {exc}")

    def connect_all_aotfs(self) -> None:
        try:
            if not self.devices:
                raise RuntimeError("Load a setup first")
            self.transports.connect_all(self.devices, self.setup)
            names = ", ".join(self.transports.connected_names)
            self.aotf_status_label.setText(f"Connected: {names}")
            self.log(f"Connected all configured AA AOTF serial devices: {names}")
        except Exception as exc:
            self.aotf_status_label.setText("Connection failed")
            self.show_error("Could not connect AOTFs", exc)

    def disconnect_aotfs(self) -> None:
        if self.transports.connected_names:
            errors = self.transports.disable_all(self.devices)
            for error in errors:
                self.log(f"WARNING while switching off: {error}")
            self.transports.close()
            self.log("AOTF serial connections closed")
        self.aotf_status_label.setText("Not connected")

    def connect_power_meter(self) -> None:
        try:
            if self.power_meter is not None:
                self.power_meter.close()
            self.power_meter = PM100D(
                serial=self.pm_serial_edit.text(),
                backend=self.pm_backend_combo.currentData(),
            )
            self.power_meter.connect()
            identity = self.power_meter.identify()
            self.pm_status_label.setText("Connected")
            self.log(
                f"PM100D connected at {self.power_meter.resource_name}: {identity}"
            )
            if self.devices:
                device = self.selected_device()
                self.power_meter.set_wavelength_nm(device.wavelength_nm)
                self.log(
                    f"PM100D wavelength set to {device.wavelength_nm:g} nm"
                )
        except Exception as exc:
            self.pm_status_label.setText("Connection failed")
            if self.power_meter is not None:
                self.power_meter.close()
            self.show_error("Could not connect PM100D", exc)

    def require_power_meter(self) -> PM100D:
        if self.power_meter is None or not self.power_meter.connected:
            raise RuntimeError("Connect the PM100D first")
        return self.power_meter

    def zero_power_meter(self) -> None:
        try:
            meter = self.require_power_meter()
            response = QtWidgets.QMessageBox.question(
                self,
                "Zero PM100D",
                "Block all light reaching the power-meter sensor, then choose Yes.",
            )
            if response != QtWidgets.QMessageBox.Yes:
                return
            meter.zero()
            self.log("PM100D zeroing initiated")
        except Exception as exc:
            self.show_error("Could not zero PM100D", exc)

    def read_power_meter(self) -> None:
        try:
            measured = self.require_power_meter().read_power_mw()
            self.pm_status_label.setText(f"{measured:.6g} mW")
            self.log(f"PM100D: {measured:.9g} mW")
        except Exception as exc:
            self.show_error("Could not read PM100D", exc)

    def test_selected_output(self) -> None:
        try:
            device = self.selected_device()
            self.transports.prepare(device)
            reply = self.transports.set_output(
                device,
                frequency_mhz=self.test_frequency_box.value(),
                drive=self.test_drive_box.value(),
                enabled=True,
            )
            self.log(
                f"{device.name} enabled at {self.test_frequency_box.value():g} "
                f"MHz, raw power {self.test_drive_box.value():g}; reply={reply!r}"
            )
            if self.power_meter is not None and self.power_meter.connected:
                measured = self.power_meter.read_power_mw()
                self.pm_status_label.setText(f"{measured:.6g} mW")
                self.log(f"Measured power: {measured:.9g} mW")
        except Exception as exc:
            self.show_error("Could not test AOTF", exc)

    def disable_selected_output(self) -> None:
        try:
            device = self.selected_device()
            self.transports.disable(device)
            self.log(f"{device.name} switched off")
        except Exception as exc:
            self.show_error("Could not switch off AOTF", exc)

    def run_frequency_sweep(self) -> None:
        try:
            start = self.frequency_min_box.value()
            stop = self.frequency_max_box.value()
            if stop <= start:
                raise ValueError("End frequency must be greater than start frequency")
            values = tuple(np.linspace(
                start, stop, self.frequency_steps_box.value(), endpoint=True
            ))
            spec = SweepSpec(
                kind="frequency",
                device=self.selected_device(),
                values=values,
                fixed_value=self.frequency_drive_box.value(),
                settle_seconds=self.frequency_settle_box.value(),
            )
            self.start_sweep(spec)
        except Exception as exc:
            self.show_error("Could not start frequency sweep", exc)

    def run_power_sweep(self) -> None:
        try:
            start = self.power_min_box.value()
            stop = self.power_max_box.value()
            if stop <= start:
                raise ValueError("Maximum power must be greater than minimum power")
            values = tuple(np.linspace(
                start, stop, self.power_steps_box.value(), endpoint=True
            ))
            spec = SweepSpec(
                kind="power",
                device=self.selected_device(),
                values=values,
                fixed_value=self.save_frequency_box.value(),
                settle_seconds=self.power_settle_box.value(),
            )
            self.start_sweep(spec)
        except Exception as exc:
            self.show_error("Could not start power calibration", exc)

    def start_sweep(self, spec: SweepSpec) -> None:
        if self._thread is not None:
            raise RuntimeError("A sweep is already running")
        self.transports.require(spec.device)
        meter = self.require_power_meter()
        meter.set_wavelength_nm(spec.device.wavelength_nm)

        self._thread = QtCore.QThread(self)
        self._worker = SweepWorker(spec, self.transports, meter)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.sweep_progress)
        self._worker.message.connect(self.log)
        self._worker.finished.connect(self.sweep_finished)
        self._worker.failed.connect(self.sweep_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self.sweep_thread_finished)
        self._set_sweep_controls(False)
        self.progress_bar.setValue(0)
        self.log(
            f"Starting {spec.kind} sweep for {spec.device.name}: "
            f"{len(spec.values)} point(s), settle {spec.settle_seconds:g} s"
        )
        self._thread.start()

    def _set_sweep_controls(self, enabled: bool) -> None:
        self.frequency_sweep_button.setEnabled(enabled)
        self.power_sweep_button.setEnabled(enabled)
        self.test_button.setEnabled(enabled)
        self.connect_aotfs_button.setEnabled(enabled)
        self.disconnect_aotfs_button.setEnabled(enabled)
        self.device_combo.setEnabled(enabled)
        self.cancel_button.setEnabled(not enabled)

    @QtCore.pyqtSlot(int, int, float, float)
    def sweep_progress(
        self, index: int, total: int, value: float, measured_mw: float
    ) -> None:
        self.progress_bar.setValue(round(index / total * 100))
        self.log(
            f"{index}/{total}: set {value:.9g}, measured "
            f"{measured_mw:.9g} mW"
        )

    @QtCore.pyqtSlot(str, object)
    def sweep_finished(self, kind: str, rows: list[tuple[float, float]]) -> None:
        device = self.selected_device()
        if not rows:
            self.log(f"{kind.capitalize()} sweep produced no measurements")
            return
        if kind == "frequency":
            self.frequency_results[device.name] = rows
            best_frequency, best_power = max(rows, key=lambda row: row[1])
            self.test_frequency_box.setValue(best_frequency)
            self.save_frequency_box.setValue(best_frequency)
            self.frequency_result_label.setText(
                f"Peak measured at {best_frequency:.9g} MHz "
                f"({best_power:.9g} mW). The value was copied to the save field."
            )
            self.log(
                f"Peak for {device.name}: {best_frequency:.9g} MHz at "
                f"{best_power:.9g} mW"
            )
        else:
            self.power_results[device.name] = rows
            self.log(
                f"Power calibration captured {len(rows)} point(s) for "
                f"{device.name}; choose a CSV path and save"
            )

    @QtCore.pyqtSlot(str)
    def sweep_failed(self, message: str) -> None:
        self.log(f"Sweep failed: {message}")
        QtWidgets.QMessageBox.critical(self, "Sweep failed", message)

    @QtCore.pyqtSlot()
    def sweep_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._set_sweep_controls(True)

    def cancel_sweep(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.log("Cancellation requested; the channel will be switched off")

    def browse_calibration(self) -> None:
        start = self.calibration_path_edit.text().strip()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save AOTF calibration",
            start,
            "Calibration data (*.csv *.txt)",
        )
        if path:
            if Path(path).suffix.lower() not in (".csv", ".txt"):
                path += ".csv"
            self.calibration_path_edit.setText(path)

    def save_selected_calibration(self) -> None:
        try:
            if self.setup_path is None:
                raise RuntimeError("Load a setup first")
            device = self.selected_device()
            frequency = self.save_frequency_box.value()
            minimum = self.power_min_box.value()
            maximum = self.power_max_box.value()
            calibration_path_text = self.calibration_path_edit.text().strip()
            if not calibration_path_text:
                raise ValueError("Choose a calibration CSV path")
            calibration_path = Path(calibration_path_text).expanduser().resolve()
            rows = self.power_results.get(device.name)
            if rows is None and not calibration_path.is_file():
                raise ValueError(
                    "Run a power calibration or choose an existing calibration CSV"
                )

            answer = QtWidgets.QMessageBox.question(
                self,
                "Save AOTF calibration",
                f"Update {device.name} in:\n{self.setup_path}\n\n"
                f"Frequency: {frequency:g} MHz\n"
                f"Raw power range: {minimum:g}–{maximum:g}\n"
                f"Calibration: {calibration_path}\n\n"
                "A timestamped backup of the setup will be created.",
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return

            try:
                self.transports.disable(device)
            except Exception as exc:
                self.log(f"WARNING: could not switch channel off before save: {exc}")

            if rows is not None:
                save_calibration_csv(
                    calibration_path,
                    rows,
                    laser_name=device.name,
                    wavelength_nm=device.wavelength_nm,
                    frequency_mhz=frequency,
                    notes=self.notes_edit.toPlainText(),
                )

            updated_setup = copy.deepcopy(self.setup)
            apply_calibration_to_setup(
                updated_setup,
                laser_name=device.name,
                frequency_mhz=frequency,
                value_min=minimum,
                value_max=maximum,
                calibration_path=calibration_path,
            )
            backup = save_setup_with_backup(self.setup_path, updated_setup)
            self.setup = updated_setup
            selected_name = device.name
            self.devices = discover_aotfs(self.setup)
            self.log(f"Saved calibration: {calibration_path}")
            self.log(f"Updated setup: {self.setup_path}")
            self.log(f"Setup backup: {backup}")
            QtWidgets.QMessageBox.information(
                self,
                "Calibration saved",
                f"Updated {selected_name}.\n\nBackup:\n{backup}",
            )
        except Exception as exc:
            self.show_error("Could not save calibration", exc)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._worker is not None:
            self._worker.cancel()
            errors = self.transports.disable_all(self.devices)
            for error in errors:
                self.log(f"WARNING while switching off: {error}")
            self.log(
                "Close requested during a sweep. Cancellation is pending; "
                "close the window again after the sweep stops."
            )
            event.ignore()
            return
        errors = self.transports.disable_all(self.devices)
        for error in errors:
            self.log(f"WARNING while switching off: {error}")
        self.transports.close()
        if self.power_meter is not None:
            self.power_meter.close()
        event.accept()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "setup",
        nargs="?",
        type=Path,
        help="Optional ImSwitch setup JSON to open",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = QtWidgets.QApplication(sys.argv[:1])
    window = AOTFCalibrationWindow(args.setup)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
