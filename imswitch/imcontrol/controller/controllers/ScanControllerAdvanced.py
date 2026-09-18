# ScanControllerLineStepPointScan.py
import copy
import json
import traceback
from typing import Dict, Any

import numpy as np
from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.scan_parameters import (
    AdvancedScanParameterSerializer,
    pixels_for_length_step,
)
from ..basecontrollers import SuperScanController

# Optional: only if you want wavelength-based colors like MoNaLISA
from imswitch.imcommon.view.guitools import colorutils
from ...model import SignalDesignerFactory


class ScanControllerAdvanced(SuperScanController):
    """
    Controller for a PointScan-like scan widget with:
      - line-step laser combinations (repeat same line multiple times with different device enables)
      - optional advanced intra-pixel pulse timing per device per linestep
      - TTL preview graph (stationary TTL generation, no scanInfo required)

    Digital parameter dict format (new, no backwards compat):
      target_device: list[str]
      n_linesteps: int
      linestep_enable: dict[str, list[bool]]
      pulse_starts_s: dict[str, list[list[float]]]
      pulse_ends_s: dict[str, list[list[float]]]
      sequence_time: float   # dwell time per pixel (sec)
      advanced_mode: bool
    """


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Snapshot of the parameters that produced the cached signalDict, so
        # repeated scan frames can skip regenerating an identical signal.
        self._lastBuiltParams = None

        # Widget-state <-> scan-dict translation lives in a controller-free,
        # unit-testable serializer (audit 07). The controller binds widgets to it.
        self._scanParams = AdvancedScanParameterSerializer()

        # ---- widget init ----
        # Keep PointScan signature (pos, TTL devices)
        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys(),
            "ms"
        )

        # Tell the widget which TTL devices support per-linestep analog power (AO channel present)
        try:
            power_capable = []
            for name, info in getattr(self._setupInfo, "lasers", {}).items():
                ao = getattr(info, "analogChannel", None)
                if ao not in (None, "None"):
                    power_capable.append(name)
            self._widget.setLinestepPowerCapableDevices(power_capable)
        except Exception:
            self._logger.debug("Could not detect power-capable devices:\n%s", traceback.format_exc())

        # ---- initial state ----
        self.updatePixels()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        # ---- plotting hooks (optional, depends on your widget) ----
        for sig_name in ("sigSeqTimeParChanged", "sigSignalParChanged"):
            sig = getattr(self._widget, sig_name, None)
            if sig is not None:
                sig.connect(self.plotSignalGraph)
        if hasattr(self._widget, 'sigStageParChanged'):
            self._widget.sigStageParChanged.connect(self.updatePixels)
        if hasattr(self._widget, "sigPlotScanClicked"):
            self._widget.sigPlotScanClicked.connect(self.plotScanCurves)

        # Try initial plot
        try:
            self.plotSignalGraph()
        except Exception:
            self._logger.debug("[ScanControllerAdvanced] initial plotSignalGraph failed:\n%s", traceback.format_exc())

        # Register for widget state persistence
        getWidgetStatePersistence().register('Scan', self)

    # ---------------------------------------------------------------------
    # Internal helpers: designer instances (no ScanManager in this branch)
    # ---------------------------------------------------------------------

    def _get_scan_designer(self):
        if not getattr(self._setupInfo, "scan", None):
            raise RuntimeError("setupInfo.scan is not defined; cannot scan")
        return SignalDesignerFactory(self._setupInfo.scan.scanDesigner)

    def _get_ttl_designer(self):
        if not getattr(self._setupInfo, "scan", None):
            raise RuntimeError("setupInfo.scan is not defined; cannot scan")
        return SignalDesignerFactory(self._setupInfo.scan.TTLCycleDesigner)

    def _make_full_scan(self, scanParameters, TTLParameters):
        """
        Constructs the full scan from scan parameters using the set parameters in the ScanWidgetAdvanced
        Returns:
          signalDict = {'scanSignalsDict': ..., 'TTLCycleSignalsDict': ...}
          scanInfoDict
        """
        scan_des = self._get_scan_designer()
        ttl_des = self._get_ttl_designer()

        # --- stage / analog ---
        stage_param = copy.deepcopy(getattr(self._setupInfo.scan, "scanDesignerParams", {}))
        stage_param.update(scanParameters)
        stage_param["n_linesteps"] = int(TTLParameters.get("n_linesteps", 1))
        self._copy_positioner_line_program_to_stage_params(stage_param, TTLParameters)

        # optional guard (like PointScan)
        if hasattr(scan_des, "checkSignalLength"):
            if not scan_des.checkSignalLength(scanParameters, self._setupInfo):
                self._logger.error(
                    "Signal too long: try scanning a smaller ROI, faster, or with a larger pixel size."
                )
                return None, None

        scanSignalsDict, positions, scanInfoDict = scan_des.make_signal(stage_param, self._setupInfo)

        # --- TTL / digital ---
        ttl_param = copy.deepcopy(getattr(self._setupInfo.scan, "TTLCycleDesignerParams", {}))
        ttl_param.update(self._ttl_parameters_without_positioners(TTLParameters))

        TTLCycleSignalsDict, scanInfoDict = ttl_des.make_signal(ttl_param, self._setupInfo, scanInfoDict)

        # scanInfoDict is already a complete ScanInfoContract dict from the scan designer.
        # No normalization or finalization needed.

        # Inject per-linestep analog power waveforms for AO-capable lasers (constant within each line)
        if TTLParameters.get("advanced_mode", False):
            try:
                self._inject_linestep_power_ao(
                    scanSignalsDict, TTLCycleSignalsDict, scanInfoDict, TTLParameters
                )
            except Exception:
                self._logger.debug(
                    "[ScanControllerAdvanced] inject linestep power failed:\n%s",
                    traceback.format_exc()
                )

        signalDict = {
            "scanSignalsDict": scanSignalsDict,
            "TTLCycleSignalsDict": TTLCycleSignalsDict,
        }

        self._log_linestep_scan_diagnostics(
            TTLParameters=TTLParameters,
            scanSignalsDict=scanSignalsDict,
            TTLCycleSignalsDict=TTLCycleSignalsDict,
            scanInfoDict=scanInfoDict,
        )

        self._lastScanInfoDict = scanInfoDict
        self._lastSignalDict = signalDict
        self._lastTTLCycleSignalsDict = signalDict.get("TTLCycleSignalsDict", None)
        self._lastTTLParameters = copy.deepcopy(TTLParameters)

        return signalDict, scanInfoDict

    def _log_linestep_scan_diagnostics(
        self,
        *,
        TTLParameters,
        scanSignalsDict,
        TTLCycleSignalsDict,
        scanInfoDict,
    ):
        """Log the flattened-line contract at the Advanced Scan boundary."""
        requested_steps = max(
            1, int((TTLParameters or {}).get("n_linesteps", 1))
        )
        if requested_steps <= 1:
            return

        scan_dims = [
            int(value) for value in scanInfoDict.get("img_dims", [])
        ]
        scan_steps = max(
            1, int(scanInfoDict.get("n_linesteps", 1))
        )
        ui_ny = (TTLParameters or {}).get("Ny")
        physical_ny = scan_dims[1] if len(scan_dims) > 1 else None
        expected_line_periods = (
            int(np.prod(scan_dims[1:], dtype=np.int64)) * scan_steps
            if len(scan_dims) > 1 else None
        )

        line_clock = np.asarray(
            (TTLCycleSignalsDict or {}).get("line_clock", []),
            dtype=bool,
        ).reshape(-1)
        line_clock_edges = (
            int(bool(line_clock[0]))
            + int(np.count_nonzero(line_clock[1:] & ~line_clock[:-1]))
            if line_clock.size else None
        )
        scan_total = int(scanInfoDict.get("scan_samples_total", 0))
        stage_lengths = {
            name: int(np.asarray(signal).size)
            for name, signal in (scanSignalsDict or {}).items()
        }
        ttl_lengths = {
            name: int(np.asarray(signal).size)
            for name, signal in (TTLCycleSignalsDict or {}).items()
        }

        mismatches = []
        if scan_steps != requested_steps:
            mismatches.append(
                f"requested_S={requested_steps}!=scan_S={scan_steps}"
            )
        if (
            ui_ny is not None
            and physical_ny is not None
            and int(ui_ny) != physical_ny
        ):
            mismatches.append(f"UI_Ny={ui_ny}!=physical_Ny={physical_ny}")
        if (
            line_clock_edges is not None
            and expected_line_periods is not None
            and line_clock_edges != expected_line_periods
        ):
            mismatches.append(
                f"line_edges={line_clock_edges}"
                f"!=expected={expected_line_periods}"
            )
        wrong_stage_lengths = {
            name: length for name, length in stage_lengths.items()
            if length != scan_total
        }
        wrong_ttl_lengths = {
            name: length for name, length in ttl_lengths.items()
            if length != scan_total
        }
        if wrong_stage_lengths:
            mismatches.append(f"stage_lengths={wrong_stage_lengths}")
        if wrong_ttl_lengths:
            mismatches.append(f"TTL_lengths={wrong_ttl_lengths}")

        self._logger.info(
            "[LineStepDiag][AdvancedScan] requested_S=%s scan_S=%s "
            "UI_Ny=%s img_dims=%s expected_flat_lines=%s "
            "line_clock_edges=%s scan_samples_total=%s "
            "scan_samples_d2_period=%s scan_samples=%s "
            "stage_lengths=%s TTL_lengths=%s status=%s",
            requested_steps,
            scan_steps,
            ui_ny,
            scan_dims,
            expected_line_periods,
            line_clock_edges,
            scan_total,
            scanInfoDict.get("scan_samples_d2_period"),
            scanInfoDict.get("scan_samples"),
            stage_lengths,
            ttl_lengths,
            "OK" if not mismatches else "MISMATCH: " + "; ".join(mismatches),
        )

    def _copy_positioner_line_program_to_stage_params(self, stage_param, TTLParameters):
        """Forward intra-pixel positioner program metadata to scan designers that understand it."""
        for key in (
            "intra_pixel_positioner_movement",
            "positioner_target_device",
            "positioner_linestep_enable",
            "positioner_movement_starts_s",
            "positioner_movement_ends_s",
            "positioner_step_size_um",
        ):
            if key in (TTLParameters or {}):
                stage_param[key] = copy.deepcopy(TTLParameters[key])

    def _ttl_parameters_without_positioners(self, TTLParameters):
        """Keep scanning positioners out of the TTL designer target list."""
        out = copy.deepcopy(TTLParameters or {})
        ttl_device_names = set(self.TTLDevices.keys())
        out["target_device"] = [
            dev for dev in list(out.get("target_device", []) or [])
            if dev in ttl_device_names
        ]

        for dict_key in (
            "linestep_enable",
            "pulse_starts_s",
            "pulse_ends_s",
            "linestep_power_percent",
        ):
            values = out.get(dict_key, None)
            if isinstance(values, dict):
                out[dict_key] = {
                    dev: value for dev, value in values.items()
                    if dev in ttl_device_names
                }

        return out

    def _make_scan_only(self, scanParameters, TTLParameters):
        scan_des = self._get_scan_designer()
        stage_param = copy.deepcopy(getattr(self._setupInfo.scan, "scanDesignerParams", {}))
        stage_param.update(scanParameters)
        stage_param["n_linesteps"] = int((TTLParameters or {}).get("n_linesteps", 1))
        self._copy_positioner_line_program_to_stage_params(stage_param, TTLParameters)
        return scan_des.make_signal(stage_param, self._setupInfo)

    def plotScanCurves(self):
        """Build and plot analog scan curves without starting hardware tasks."""
        try:
            if getattr(self, "settingParameters", False):
                return

            self.getParameters()
            try:
                include_ttl = bool(self._widget.isPlotTTLIncluded())
            except Exception:
                include_ttl = False

            ttlSignalsDict = None
            if include_ttl:
                signalDict, scanInfoDict = self._make_full_scan(
                    self._analogParameterDict, self._digitalParameterDict
                )
                if signalDict is None:
                    return
                scanSignalsDict = signalDict.get("scanSignalsDict", {})
                ttlSignalsDict = signalDict.get("TTLCycleSignalsDict", {})
            else:
                scanSignalsDict, _, scanInfoDict = self._make_scan_only(
                    self._analogParameterDict, self._digitalParameterDict
                )
            if not scanSignalsDict:
                self._logger.warning("No scan curves to plot")
                return

            ordered_devices = [
                dev for dev in self._analogParameterDict.get("scan_dim_target_device", [])
                if dev != "None" and dev in scanSignalsDict
            ]
            if not ordered_devices:
                ordered_devices = [
                    dev for dev in self._analogParameterDict.get("target_device", [])
                    if dev in scanSignalsDict
                ]
            if not ordered_devices:
                self._logger.warning("No active scan axes to plot")
                return

            import matplotlib.pyplot as plt

            n_axes = len(ordered_devices)
            fig, axes = plt.subplots(
                n_axes,
                1,
                sharex=True,
                squeeze=False,
                figsize=(12, max(3, 2.2 * n_axes)),
            )
            axes = axes[:, 0]

            sample_rate = float(getattr(self._setupInfo.scan, "sampleRate", 1.0))
            ttl_handles = []
            ttl_labels = []
            for ax, dev in zip(axes, ordered_devices):
                signal = np.asarray(scanSignalsDict[dev], dtype=float)
                t_s = np.arange(signal.size) / sample_rate
                ax.plot(t_s, signal, color="black", linewidth=0.8, label=dev)
                ax.set_ylabel(dev)
                ax.grid(True, alpha=0.25)

                if include_ttl and ttlSignalsDict:
                    handles, labels = self._plot_ttl_overlay_on_axis(
                        ax, ttlSignalsDict, sample_rate, signal.size, signal
                    )
                    if not ttl_handles:
                        ttl_handles = handles
                        ttl_labels = labels

            axes[-1].set_xlabel("Time (s)")
            if ttl_handles:
                fig.legend(
                    ttl_handles,
                    ttl_labels,
                    loc="upper right",
                    bbox_to_anchor=(0.99, 0.99),
                    fontsize="small",
                )
            fig.suptitle("Scan Curves" + (" + TTL" if include_ttl else ""))
            fig.tight_layout()
            try:
                fig.canvas.manager.set_window_title("ImSwitch Scan Curves")
            except Exception:
                pass
            plt.show(block=False)

            self._lastPlottedScanInfoDict = scanInfoDict
            self._lastPlottedScanSignalsDict = scanSignalsDict
            self._lastPlottedTTLCycleSignalsDict = ttlSignalsDict
        except Exception:
            self._logger.error("[ScanControllerAdvanced] plotScanCurves failed:\n%s", traceback.format_exc())

    def _plot_ttl_overlay_on_axis(self, ax, ttlSignalsDict, sample_rate, max_samples, scan_signal):
        """Overlay full-scan TTL traces from the scan curve start level."""
        ttl_devices = [
            dev for dev in self.TTLDevices.keys()
            if dev in (ttlSignalsDict or {})
        ]
        active_targets = set(self._digitalParameterDict.get("target_device", []) or [])
        if active_targets:
            ttl_devices = [dev for dev in ttl_devices if dev in active_targets]

        handles = []
        labels = []

        ymin, ymax = ax.get_ylim()
        if ymin == ymax:
            ymin -= 0.5
            ymax += 0.5
        yrange = ymax - ymin
        scan_signal = np.asarray(scan_signal, dtype=float)
        ttl_low = float(scan_signal[0]) if scan_signal.size else ymin
        ttl_high = ttl_low + (yrange / 3.0)

        for dev in ttl_devices:
            signal = np.asarray(ttlSignalsDict[dev], dtype=float)
            n = min(int(max_samples), signal.size)
            if n <= 0:
                continue

            t_s = np.arange(n) / sample_rate
            y = np.where(signal[:n] > 0, ttl_high, ttl_low)
            line, = ax.step(
                t_s,
                y,
                where="post",
                linewidth=0.9,
                alpha=0.85,
                color=self._ttl_plot_color(dev),
                label=dev,
            )
            handles.append(line)
            labels.append(dev)

        ax.set_ylim(min(ymin, ttl_low), max(ymax, ttl_high))
        return handles, labels

    def _ttl_plot_color(self, deviceName):
        try:
            if deviceName in getattr(self._setupInfo, "lasers", {}):
                return colorutils.wavelengthToHex(
                    self._setupInfo.lasers[deviceName].wavelength,
                    gamma=6.0,
                )
        except Exception:
            pass

        lowered = str(deviceName).lower()
        if "camera" in lowered or "cam" in lowered:
            return "#864f1c"
        return "#4dabf7"

    # ---------------------------------------------------------------------
    # Scan geometry interface consumed by BeadRecController
    # ---------------------------------------------------------------------

    def getDimsScan(self):
        """Return (x, y, z) pixel counts for each scan axis (0 if axis not active)."""
        self.getParameters()
        lengths = self._analogParameterDict.get('axis_length', [])
        stepSizes = self._analogParameterDict.get('axis_step_size', [])
        dims = []
        for i in range(min(3, len(lengths))):
            step = stepSizes[i] if i < len(stepSizes) else 0
            # round(len/step) via the canonical helper (0 = inactive axis) so the
            # recorded OME dims match the GUI count and the real scanned lines.
            dims.append(pixels_for_length_step(lengths[i], step) if step != 0 else 0)
        # pad to 3 elements
        while len(dims) < 3:
            dims.append(0)
        return tuple(dims[:3])

    def getScanStepSizes(self):
        """Return step sizes for the first 3 scan axes (matching getDimsScan() length).

        BeadRecController indexes into this list with a boolean mask derived from
        getDimsScan(), so both methods must return the same number of elements (3).
        Virtual axes beyond index 2 (e.g. timelapse, repeat) are excluded.
        """
        stepSizes = self._analogParameterDict.get('axis_step_size', [])
        result = list(stepSizes[:3])
        while len(result) < 3:
            result.append(0.0)
        return result

    # ---------------------------------------------------------------------
    # Parameters: UI -> dicts
    # ---------------------------------------------------------------------

    def getParameters(self):
        """
        Populates:
          self._analogParameterDict
          self._digitalParameterDict
        from widget state.

        Analog format kept compatible with GalvoScanDesigner expectedParameters.
        Digital format expected by AdvancedScanTTLCycleDesigner.
        """
        if getattr(self, "settingParameters", False):
            return

        self._analogParameterDict, self._positionersScan = (
            self._buildAnalogParameterDict()
        )
        self._digitalParameterDict = self._buildDigitalParameterDict(
            self._analogParameterDict
        )

    def _buildAnalogParameterDict(self):
        """Serialize PointScan-compatible analog scan parameters from the widget."""
        return self._scanParams.build_analog(self._widget, self.positioners)

    def _pixelsForScanDevice(self, analogParameterDict, deviceName: str) -> int:
        """Return pixel count for a selected scan device from analog params."""
        return self._scanParams.pixels_for_scan_device(analogParameterDict, deviceName)

    def _buildDigitalParameterDict(self, analogParameterDict):
        """Serialize advanced TTL and line-step parameters from the widget."""
        return self._scanParams.build_digital(
            self._widget, analogParameterDict, self.positioners, self.TTLDevices
        )

    # ---------------------------------------------------------------------
    # Parameters: dicts -> UI (used by loadScan)
    # ---------------------------------------------------------------------

    def setParameters(self):
        self.settingParameters = True
        try:
            self._scanParams.apply(
                self._widget,
                self._analogParameterDict,
                self._digitalParameterDict,
                self.positioners,
                self.TTLDevices,
            )
        finally:
            self.settingParameters = False
            try:
                self.updatePixels()
                self.plotSignalGraph()
            except Exception:
                self._logger.debug("[ScanControllerAdvanced] setParameters follow-up failed:\n%s", traceback.format_exc())

    # ---------------------------------------------------------------------
    # Scan run
    # ---------------------------------------------------------------------

    def runScanAdvanced(
        self,
        *,
        recalculateSignals=True,
        isNonFinalPartOfSequence=False,
        sigScanStartingEmitted=False,
    ):
        """Runs a scan with current parameters."""
        try:
            if self._beginScanRun(
                sigScanStartingEmitted=sigScanStartingEmitted
            ) is None:
                return
            self._widget.setScanButtonChecked(True)

            if recalculateSignals or self.signalDict is None or self.scanInfoDict is None:
                self.getParameters()

                # Only rebuild the (expensive) scan signal if the parameters
                # actually changed since the last build. Repeated scan frames
                # reuse identical parameters, so this avoids regenerating a
                # byte-identical galvo/TTL signal — and the per-frame stall it
                # causes — on every repeat. Live parameter edits still trigger
                # a rebuild because the snapshot then differs.
                paramsSnapshot = (
                    copy.deepcopy(self._analogParameterDict),
                    copy.deepcopy(self._digitalParameterDict),
                )
                signalsCached = (
                    self.signalDict is not None
                    and self.scanInfoDict is not None
                    and paramsSnapshot == self._lastBuiltParams
                )
                if not signalsCached:
                    # TTL cycle (linestep_enable) is the sole authority for per-laser emission
                    self.signalDict, self.scanInfoDict = self._make_full_scan(
                        self._analogParameterDict, self._digitalParameterDict
                    )

                    if self.signalDict is None:
                        self.scanFailed()
                        return

                    self._lastBuiltParams = paramsSnapshot

            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence

            # Set non-scanned positioners to center (same behavior as your PointScan controller)
            for index, positionerName in enumerate(self._analogParameterDict["target_device"]):
                if positionerName not in self._positionersScan:
                    try:
                        position = self._analogParameterDict["axis_centerpos"][index]
                        self._master.positionersManager[positionerName].setPosition(position, 0)
                    except Exception:
                        self._logger.warning("Failed to set %s to center:\n%s",
                                             positionerName, traceback.format_exc())

            self._armScanIteration(self.signalDict, self.scanInfoDict)

        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def scanDone(self):
        """Called by the system when nidaq finishes."""
        self.isRunning = False
        try:
            if not self._widget.repeatEnabled():
                isFinalPart = not getattr(
                    self, "doingNonFinalPartOfSequence", False
                )
                self._restoreScanPositioners()
                if isFinalPart:
                    try:
                        self._widget.setScanButtonChecked(False)
                    except Exception:
                        self._logger.error(
                            'Failed to reset the scan widget after completion',
                            exc_info=True,
                        )
                self._publishScanDone(isFinalPart=isFinalPart)
            else:
                # Defer the re-arm so the finished scan's NI-DAQ tasks and
                # detector threads tear down before the next frame starts.
                self._armRepeatScan()
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    # ---------------------------------------------------------------------
    # Pixel counting
    # ---------------------------------------------------------------------

    def updatePixels(self):
        self.getParameters()
        try:
            for index, positionerName in enumerate(self._analogParameterDict["target_device"]):
                step = float(self._analogParameterDict["axis_step_size"][index])
                if step != 0:
                    length = float(self._analogParameterDict["axis_length"][index])
                    pixels = pixels_for_length_step(length, step)
                    self._widget.setScanPixels(positionerName, pixels)
        except Exception:
            self._logger.debug("updatePixels failed:\n%s", traceback.format_exc())

    # ---------------------------------------------------------------------
    # TTL preview plotting
    # ---------------------------------------------------------------------

    def plotSignalGraph(self):
        """
        Preview plots for Advanced widget:
          - graph_steps: scatter of enabled linesteps per device (length S)
          - graph_pixel: per-pixel TTL program constructed inside widget from pulse model
        """
        if getattr(self, "settingParameters", False):
            return

        try:
            self.getParameters()

            if not getattr(self._setupInfo, "scan", None):
                return

            sampleRate = self._setupInfo.scan.sampleRate

            # device order = stable order of TTLDevices
            labels = list(self.TTLDevices.keys())

            # number of linesteps (S)
            try:
                S = int(self._widget.getNumLineSteps())
            except Exception:
                S = int(self._digitalParameterDict.get("n_linesteps", 1))

            # build per-device enable vectors (length S)
            signals = []
            for dev in labels:
                try:
                    enable_vec = [bool(self._widget.getLineStepEnabled(dev, s)) for s in range(S)]
                except Exception:
                    enable_vec = [bool(self._widget.getTTLIncluded(dev))] + [False] * (S - 1)
                signals.append(np.asarray(enable_vec, dtype=bool))

            # colors (lasers get wavelength color, others white)
            colors = []
            for dev in labels:
                isLaser = dev in getattr(self._setupInfo, "lasers", {})
                colors.append(
                    colorutils.wavelengthToHex(self._setupInfo.lasers[dev].wavelength, gamma=6.0)
                    if isLaser else "#ffffff"
                )

            # Let the widget render BOTH plots:
            #  - graph_steps uses "signals" (length S)
            #  - graph_pixel uses the pulse editor / UI state internally
            self._widget.plotSignalGraph(signals, colors, sampleRate, labels=labels)

        except Exception:
            self._logger.debug(
                "[ScanControllerAdvanced] plotSignalGraph failed:\n%s",
                traceback.format_exc(),
            )

    def _inject_linestep_power_ao(self, scanSignalsDict, TTLCycleSignalsDict, scanInfoDict, TTLParameters):
        """
        Create AO waveforms for AO-capable lasers:
        - constant voltage during each line's active part
        - line index -> linestep index via (line_idx % S)
        - aligned using the generated line_clock (most robust across axis configs)
        """
        powers = (TTLParameters or {}).get("linestep_power_percent", {}) or {}
        power_enabled = (TTLParameters or {}).get("linestep_power_enabled", {}) or {}
        if not powers:
            return

        S = int((TTLParameters or {}).get("n_linesteps", 1))
        S = max(1, S)

        total = int(scanInfoDict.get("scan_samples_total", 0))
        if total <= 0:
            return

        # line geometry from scanInfo
        scan_samples = scanInfoDict.get("scan_samples", None)
        if not isinstance(scan_samples, (list, tuple)) or len(scan_samples) < 2:
            return

        line_len = int(scan_samples[1])
        period_len = int(scanInfoDict.get("scan_samples_d2_period", 0)) or line_len
        flyback = max(0, period_len - line_len)

        # Use line_clock to find line starts (best alignment)
        line_clock = TTLCycleSignalsDict.get("line_clock", None)
        if line_clock is None:
            # fallback: assume starts every period_len from 0
            line_starts = np.arange(0, total, period_len, dtype=int)
        else:
            lc = np.asarray(line_clock, dtype=bool)
            # rising edges mark new line
            rises = np.flatnonzero(np.logical_and(lc[1:], ~lc[:-1])) + 1
            # if clock starts high at index 0
            if lc.size and lc[0]:
                rises = np.concatenate(([0], rises))
            line_starts = rises.astype(int)

        for laserName, vec in powers.items():
            # Missing flag means enabled for backward compatibility with scans
            # saved before power modulation became optional.
            if not bool(power_enabled.get(laserName, True)):
                continue

            laserInfo = getattr(self._setupInfo, "lasers", {}).get(laserName, None)
            if laserInfo is None:
                continue

            ao_chan = getattr(laserInfo, "analogChannel", None)
            if ao_chan in (None, "None"):
                continue

            vec = list(vec) if vec is not None else [100.0] * S
            if len(vec) < S:
                vec = vec + [vec[-1] if vec else 100.0] * (S - len(vec))
            vec = [max(0.0, min(100.0, float(v))) for v in vec[:S]]

            vmin = float(getattr(laserInfo, "valueRangeMin", 0.0))
            vmax = float(getattr(laserInfo, "valueRangeMax", 10.0))

            ao = np.zeros(total, dtype=np.float64)

            for line_idx, i0 in enumerate(line_starts):
                s = line_idx % S
                pct = vec[s]
                volts = vmin + (pct / 100.0) * (vmax - vmin)

                j0 = int(i0)
                j1 = min(total, j0 + line_len)
                if j1 > j0:
                    ao[j0:j1] = volts
                # flyback remains 0 by default

            # Mask by TTL if present (keeps AO at 0 when laser is off)
            mask = TTLCycleSignalsDict.get(laserName, None)
            if mask is not None:
                ao *= np.asarray(mask, dtype=np.float64)

            scanSignalsDict[laserName] = ao

    # ---------------------------------------------------------------------
    # Save / Load
    # ---------------------------------------------------------------------

    @APIExport(runOnUIThread=True)
    def changeScanCenterPos(self, positionerName, positionerScanCenterPos):
        self._widget.setScanCenterPos(positionerName, positionerScanCenterPos)

    @APIExport(runOnUIThread=True)
    def changeScanSize(self, positioner: str, size: float):
        self._widget.setScanSize(positioner, size)

    # ------------------------------------------------------------------
    # Widget State Persistence Interface
    # ------------------------------------------------------------------

    def getStateSchemaVersion(self) -> int:
        return 1

    def getNumLineSteps(self) -> int:
        """Return the number of linesteps in the scan. Returns n_linesteps from digitalParameterDict."""
        return int(self._digitalParameterDict.get("n_linesteps", 1))

    def getFramesPerScanPixel(self) -> int:
        """Return the number of detector frames produced per physical scan pixel.

        The detector only acquires during linesteps where its TTL is enabled,
        firing once per pixel in each enabled linestep. This counts the enabled
        linesteps of the detector device(s) found in linestep_enable. Assumes
        one camera trigger per pixel per enabled linestep; advanced multi-pulse
        camera waveforms within a single linestep are not accounted for.
        """
        enable = self._digitalParameterDict.get("linestep_enable", {}) or {}
        counts = [
            sum(map(bool, vec))
            for dev, vec in enable.items()
            if dev in self._setupInfo.detectors
        ]
        return max(1, max(counts)) if counts else 1
