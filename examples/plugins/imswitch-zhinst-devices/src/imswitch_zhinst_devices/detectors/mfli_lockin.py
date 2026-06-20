# SPDX-License-Identifier: GPL-3.0-or-later
"""Zurich Instruments lock-in demodulator detector manager.

The manager represents a stream of demodulator samples as a small 2D detector
frame. This keeps the first plugin example compatible with ImSwitch's current
DetectorManager contract while leaving room for a future time-series detector
contract.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Any

import numpy as np

try:
    from imswitch.pluginapi import (
        DetectorAction,
        DetectorListParameter,
        DetectorManager,
        DetectorNumberParameter,
    )
except ImportError:  # Compatibility until imswitch.pluginapi exists.
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
        DetectorAction,
        DetectorListParameter,
        DetectorManager,
        DetectorNumberParameter,
    )

try:
    from imswitch.imcommon.model import initLogger
except Exception:  # pragma: no cover - useful only outside ImSwitch.

    def initLogger(owner, instanceName=None):
        name = getattr(owner, "__class__", type(owner)).__name__
        if instanceName:
            name = f"{name}.{instanceName}"
        return logging.getLogger(name)


SUPPORTED_SIGNALS = ("x", "y", "r", "theta", "phase", "frequency")


class ZhinstLockinDetectorManager(DetectorManager):
    """Detector manager for Zurich Instruments lock-in demodulator samples.

    The real hardware path uses ``zhinst-toolkit``:

    - create a ``Session`` to the LabOne data server,
    - connect the configured device serial,
    - subscribe to ``device.demods[demodIndex].sample``,
    - poll samples on each frame request.

    The mock path generates deterministic sine/cosine samples and is suitable
    for plugin discovery, UI smoke tests, and CI without LabOne.
    """

    _force_mock = False

    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        props = detectorInfo.managerProperties or {}

        self._device_serial = str(props.get("deviceSerial", "DEVXXXX"))
        self._server_host = str(props.get("serverHost", "localhost"))
        self._server_port = props.get("serverPort", None)
        self._hf2 = _optional_bool(props.get("hf2", None))
        self._interface = props.get("interface", None)
        self._allow_version_mismatch = bool(props.get("allowVersionMismatch", False))
        self._demod_index = int(props.get("demodIndex", 0))
        self._signal = _validate_signal(props.get("signal", "r"))
        self._sample_count = max(1, int(props.get("sampleCount", 256)))
        self._frame_shape = _coerce_frame_shape(props.get("frameShape"), self._sample_count)
        self._poll_duration_s = max(0.001, float(props.get("pollDurationS", 0.05)))
        self._poll_timeout_s = max(0.001, float(props.get("pollTimeoutS", 0.5)))
        self._enable_demodulator = bool(props.get("enableDemodulator", True))
        self._demod_rate_hz = props.get("demodRateHz", None)
        self._use_mock_on_failure = bool(props.get("useMockOnFailure", True))
        self._mock_mode = bool(props.get("useMock", False) or self._force_mock)
        self._mock_amplitude = float(props.get("mockAmplitude", 1.0))
        self._mock_frequency_hz = float(props.get("mockFrequencyHz", 7.0))
        self._mock_noise_std = max(0.0, float(props.get("mockNoiseStd", 0.02)))
        self._mock_sample_rate_hz = max(1.0, float(props.get("mockSampleRateHz", 1000.0)))
        self._mock_rng = np.random.default_rng(int(props.get("mockSeed", 1)))
        self._mock_sample_index = 0

        self._session = None
        self._device = None
        self._sample_node = None
        self._subscribed = False
        self._running = False
        self._chunk_queue: deque[np.ndarray] = deque()

        self._latest_frame = np.zeros(self._frame_shape, dtype=np.float32)
        frame_height, frame_width = self._frame_shape
        self._full_shape = (frame_width, frame_height)

        parameters = {
            "Signal": DetectorListParameter(
                group="Zurich Instruments",
                value=self._signal,
                options=list(SUPPORTED_SIGNALS),
                editable=True,
            ),
            "Sample count": DetectorNumberParameter(
                group="Zurich Instruments",
                value=float(self._sample_count),
                valueUnits="samples",
                editable=True,
            ),
            "Poll duration": DetectorNumberParameter(
                group="Zurich Instruments",
                value=float(self._poll_duration_s),
                valueUnits="s",
                editable=True,
            ),
            "Demodulator index": DetectorNumberParameter(
                group="Zurich Instruments",
                value=float(self._demod_index),
                valueUnits="index",
                editable=False,
            ),
        }

        actions = {
            "Reconnect": DetectorAction(
                group="Zurich Instruments",
                func=self.reconnect,
            )
        }

        model = "Zurich Instruments lock-in demodulator"
        if self._mock_mode:
            model += " (mock)"

        super().__init__(
            detectorInfo,
            name,
            fullShape=self._full_shape,
            supportedBinnings=[1],
            model=model,
            parameters=parameters,
            actions=actions,
            croppable=False,
        )

    @property
    def dtype(self):
        return np.dtype(np.float32)

    def crop(self, hpos, vpos, hsize, vsize):
        self._frameStart = (0, 0)
        self._shape = self._full_shape

    def startAcquisition(self):
        self._running = True
        self._chunk_queue.clear()
        if not self._mock_mode:
            self._ensure_real_connection()
            self._subscribe()

    def stopAcquisition(self):
        self._running = False
        self._unsubscribe()

    def finalize(self):
        self.stopAcquisition()
        self._disconnect()

    def reconnect(self):
        was_running = self._running
        self.stopAcquisition()
        self._disconnect()
        self._mock_mode = self._force_mock
        if was_running:
            self.startAcquisition()

    def setParameter(self, name: str, value: Any):
        super().setParameter(name, value)
        if name == "Signal":
            self._signal = _validate_signal(value)
            self.parameters[name].value = self._signal
        elif name == "Sample count":
            self._sample_count = max(1, int(value))
            self.parameters[name].value = float(self._sample_count)
        elif name == "Poll duration":
            self._poll_duration_s = max(0.001, float(value))
            self.parameters[name].value = float(self._poll_duration_s)
        return self.parameters

    def getLatestFrame(self, is_save=False):
        if self._mock_mode:
            samples = self._read_mock_samples()
        else:
            try:
                samples = self._read_real_samples()
            except Exception as exc:
                if not self._use_mock_on_failure:
                    raise
                self.__logger.warning(
                    "Falling back to Zurich Instruments mock mode after read failure: %s",
                    exc,
                    exc_info=True,
                )
                self._mock_mode = True
                samples = self._read_mock_samples()

        self._latest_frame = self._samples_to_frame(samples)
        if self._running:
            self._chunk_queue.append(self._latest_frame.copy())
        return self._latest_frame

    def getChunk(self):
        if not self._chunk_queue:
            height, width = self._frame_shape
            return np.empty((0, height, width), dtype=np.float32)
        frames = list(self._chunk_queue)
        self._chunk_queue.clear()
        return np.stack(frames, axis=0).astype(np.float32, copy=False)

    def flushBuffers(self):
        self._chunk_queue.clear()

    def _ensure_real_connection(self):
        if self._device is not None and self._session is not None:
            return

        try:
            from zhinst.toolkit import Session

            session_kwargs = {
                "allow_version_mismatch": self._allow_version_mismatch,
            }
            if self._hf2 is not None:
                session_kwargs["hf2"] = self._hf2

            if self._server_port is None:
                self._session = Session(self._server_host, **session_kwargs)
            else:
                self._session = Session(
                    self._server_host,
                    int(self._server_port),
                    **session_kwargs,
                )

            connect_kwargs = {}
            if self._interface:
                connect_kwargs["interface"] = self._interface
            self._device = self._session.connect_device(
                self._device_serial,
                **connect_kwargs,
            )

            demod = self._device.demods[self._demod_index]
            if self._enable_demodulator:
                demod.enable(1)
            if self._demod_rate_hz is not None:
                demod.rate(float(self._demod_rate_hz))
            self._sample_node = demod.sample
            self.__logger.info(
                "Connected Zurich Instruments device %s demodulator %s",
                self._device_serial,
                self._demod_index,
            )
        except Exception as exc:
            self._disconnect()
            if not self._use_mock_on_failure:
                raise
            self.__logger.warning(
                "Failed to connect Zurich Instruments device %s; using mock mode: %s",
                self._device_serial,
                exc,
                exc_info=True,
            )
            self._mock_mode = True

    def _subscribe(self):
        if self._sample_node is None or self._subscribed:
            return
        self._sample_node.subscribe()
        self._subscribed = True
        try:
            self._session.sync()
        except Exception:
            pass

    def _unsubscribe(self):
        if self._sample_node is None or not self._subscribed:
            return
        try:
            self._sample_node.unsubscribe()
        finally:
            self._subscribed = False

    def _disconnect(self):
        self._unsubscribe()
        if self._session is not None and self._device_serial:
            try:
                self._session.disconnect_device(self._device_serial)
            except Exception:
                pass
        self._sample_node = None
        self._device = None
        self._session = None

    def _read_real_samples(self):
        self._ensure_real_connection()
        if self._mock_mode:
            return self._read_mock_samples()
        self._subscribe()
        poll_result = self._session.poll(
            self._poll_duration_s,
            timeout=self._poll_timeout_s,
        )
        demod_sample = _lookup_polled_node(poll_result, self._sample_node)
        if not demod_sample:
            return np.zeros(self._sample_count, dtype=np.float32)
        return _extract_signal(demod_sample, self._signal)

    def _read_mock_samples(self):
        start = self._mock_sample_index
        stop = start + self._sample_count
        indices = np.arange(start, stop, dtype=np.float64)
        self._mock_sample_index = stop

        t = indices / self._mock_sample_rate_hz
        phase = 2 * math.pi * self._mock_frequency_hz * t
        x = self._mock_amplitude * np.cos(phase)
        y = self._mock_amplitude * np.sin(phase)

        if self._mock_noise_std > 0:
            x = x + self._mock_rng.normal(0, self._mock_noise_std, size=x.shape)
            y = y + self._mock_rng.normal(0, self._mock_noise_std, size=y.shape)

        if self._signal == "x":
            values = x
        elif self._signal == "y":
            values = y
        elif self._signal == "theta":
            values = np.arctan2(y, x)
        elif self._signal == "phase":
            values = np.rad2deg(np.arctan2(y, x))
        elif self._signal == "frequency":
            values = np.full_like(x, self._mock_frequency_hz)
        else:
            values = np.sqrt(x * x + y * y)

        # Simulate the real poll duration enough for UI smoke tests without
        # making mock acquisition painfully slow.
        time.sleep(min(self._poll_duration_s, 0.01))
        return values.astype(np.float32, copy=False)

    def _samples_to_frame(self, samples):
        values = np.asarray(samples, dtype=np.float32).ravel()
        target_size = int(np.prod(self._frame_shape))
        if values.size == 0:
            values = np.zeros(target_size, dtype=np.float32)
        elif values.size < target_size:
            values = np.pad(values, (0, target_size - values.size), mode="edge")
        elif values.size > target_size:
            values = values[-target_size:]
        return values.reshape(self._frame_shape)


class MockZhinstLockinDetectorManager(ZhinstLockinDetectorManager):
    """Forced-mock variant for plugin tests and setup templates."""

    _force_mock = True


def _optional_bool(value):
    if value is None:
        return None
    return bool(value)


def _validate_signal(value):
    signal = str(value).lower()
    if signal not in SUPPORTED_SIGNALS:
        raise ValueError(
            f"Unsupported Zurich Instruments signal '{value}'. "
            f"Expected one of {', '.join(SUPPORTED_SIGNALS)}."
        )
    return signal


def _coerce_frame_shape(value, sample_count):
    if value is not None:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("frameShape must be a two-item [height, width] list")
        height = max(1, int(value[0]))
        width = max(1, int(value[1]))
        return (height, width)

    width = int(math.ceil(math.sqrt(max(1, sample_count))))
    height = int(math.ceil(sample_count / width))
    return (height, width)


def _lookup_polled_node(poll_result, sample_node):
    if sample_node in poll_result:
        return poll_result[sample_node]

    node_path = None
    try:
        node_path = sample_node.node_info.path
    except Exception:
        pass

    for key, value in poll_result.items():
        if key is sample_node or str(key) == str(sample_node):
            return value
        if node_path is not None and str(key).lower() == str(node_path).lower():
            return value
    return None


def _extract_signal(demod_sample, signal):
    if signal in demod_sample:
        return np.asarray(demod_sample[signal], dtype=np.float32)

    x = np.asarray(demod_sample.get("x", []), dtype=np.float32)
    y = np.asarray(demod_sample.get("y", []), dtype=np.float32)

    if signal == "r":
        return np.sqrt(x * x + y * y)
    if signal == "theta":
        return np.arctan2(y, x)
    if signal == "phase":
        return np.rad2deg(np.arctan2(y, x)).astype(np.float32, copy=False)

    return np.zeros(max(len(x), len(y), 1), dtype=np.float32)
