from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from dataclasses_json import dataclass_json, Undefined, CatchAll


@dataclass(frozen=True, kw_only=True)
class DeviceInfo:
    managerName: str
    """ Manager class name. """

    analogChannel: Optional[Union[str, int]] = None
    """ Channel for analog communication. ``null`` if the device is digital or
    doesn't use NI-DAQ. If an integer is specified, it will be translated to
    "Dev1/ao{analogChannel}". """

    digitalLine: Optional[Union[str, int]] = None
    """ Line for digital communication. ``null`` if the device is analog or
    doesn't use NI-DAQ. If an integer is specified, it will be translated to
    "Dev1/port0/line{digitalLine}". """

    managerProperties: Dict[str, Any] = field(default_factory=dict)
    """ Properties to be read by the manager. Empty when omitted. """

    def getAnalogChannel(self):
        """ :meta private: """
        if isinstance(self.analogChannel, int):
            return f'Dev1/ao{self.analogChannel}'  # for backwards compatibility
        else:
            return self.analogChannel

    def getDigitalLine(self):
        """ :meta private: """
        if isinstance(self.digitalLine, int):
            return f'Dev1/port0/line{self.digitalLine}'  # for backwards compatibility
        else:
            return self.digitalLine


@dataclass(frozen=True, kw_only=True)
class DetectorInfo(DeviceInfo):
    forAcquisition: bool = False
    """ Whether the detector is used for acquisition. """

    forFocusLock: bool = False
    """ Whether the detector is used for focus lock. """


@dataclass(frozen=True, kw_only=True)
class LaserInfo(DeviceInfo):
    valueRangeMin: Optional[Union[int, float]]
    """ Minimum value of the laser. ``null`` if laser doesn't setting a value.
    """

    valueRangeMax: Optional[Union[int, float]]
    """ maximum value of the laser. ``null`` if laser doesn't setting a value.
    """

    wavelength: Union[int, float]
    """ Laser wavelength in nanometres. """

    freqRangeMin: Optional[int] = 0
    """ Minimum value of frequency modulation. Don't fill if laser doesn't support it. """

    freqRangeMax: Optional[int] = 0
    """ Minimum value of frequency modulation. Don't fill if laser doesn't support it. """

    freqRangeInit: Optional[int] = 0
    """ Initial value of frequency modulation. Don't fill if laser doesn't support it. """

    valueRangeStep: float = 1.0
    """ The default step size of the value range that the laser can be set to.
    """

    powerDevice: Optional[str] = None
    """ Name of the laser device that sets this one's emission power, when the
    two are separate hardware.

    Some beam paths split one physical laser across two entries: this one owns
    the TTL line the scan gates (often a bare digital-line placeholder with no
    power of its own), while an attenuator such as an AOTF channel sets the
    power over a serial link. Naming that partner here lets a scan switch the
    power device on for the duration, instead of the laser emitting only when
    the user happens to have enabled it by hand.

    Leave ``null`` when one entry owns both gate and power (an AOM with its own
    analog channel and digital line, for instance). """


@dataclass(frozen=True, kw_only=True)
class PositionerInfo(DeviceInfo):
    axes: List[str]
    """ A list of axes (names) that the positioner controls. """

    isPositiveDirection: bool = True
    """ Whether the direction of the positioner is positive. """

    forPositioning: bool = False
    """ Whether the positioner is used for manual positioning. """

    forScanning: bool = False
    """ Whether the positioner is used for scanning. """

    resetOnClose: bool = True
    """ Whether the positioner should be reset to 0-position upon closing ImSwitch. """

    joystick: bool = False
    """ Whether the positioner is connected to a joystick. """

    liveUpdate: bool = False
    """ Whether the positioner position should be updated live. """

    shortcutModifier: Optional[str] = None
    """ Keyboard-shortcut group used to jog this positioner from the Positioner
    widget. ``"ctrl"`` binds the Ctrl+Arrow set; ``"ctrl-shift"`` binds the
    Ctrl+Shift+Arrow set; ``null`` (default) keeps the legacy behaviour where the
    first positioner declaring a given axis claims the Ctrl+Arrow set. """




@dataclass(frozen=True)
class RS232Info:
    managerName: str
    """ RS232 manager class name. """

    managerProperties: Dict[str, Any]
    """ Properties to be read by the RS232 manager. """


@dataclass(frozen=True)
class SLMInfo:
    monitorIdx: int
    """ Index of the monitor in the system list of monitors (indexing starts at
    0). """

    width: int
    """ Width of SLM, in pixels. """

    height: int
    """ Height of SLM, in pixels. """

    wavelength: int
    """ Wavelength of the laser line used with the SLM. """

    pixelSize: float
    """ Pixel size or pixel pitch of the SLM, in millimetres. """

    correctionPatternsDir: str
    """ Directory of .bmp images provided by Hamamatsu for flatness correction
    at various wavelengths. A combination will be chosen based on the
    wavelength. """

    serial_number: Optional[str] = None
    """ Unique n° of the SLM head you use. ``null``/omitted if not needed
    (e.g. simulated SLMs). """




@dataclass(frozen=True, kw_only=True)
class SLMsInfo(DeviceInfo):
    monitorIdx: int
    """ Index of the monitor in the system list of monitors (indexing starts at
    0). """

    width: int
    """ Width of SLM, in pixels. """

    height: int
    """ Height of SLM, in pixels. """

    wavelength: int
    """ Wavelength of the laser line used with the SLM. """

    pixelSize: float
    """ Pixel size or pixel pitch of the SLM, in millimetres. """

    correctionPatternsDir: str
    """ Directory of .bmp images provided by Hamamatsu for flatness correction
    at various wavelengths. A combination will be chosen based on the
    wavelength. """

    wavelengthTableFile: str
    """ Name of JSON file with table of wavelength correction values to transform
    2pi modulation into gray values (given my manufacturer.) File is expected to be
    in the same direction than `correctionPatternsDir """

    nSections: Optional[int] = None
    """ Numbers of sections the SLM is divided into (e.g. 2 for double-pass).
    If none, considered as single section. """

    widgetOptions: Optional[Dict[str,Any]] = None
    """ Widget options just as which patterns to display """

    serial_number: Optional[str] = None
    """ Unique n° of the SLM head you use. ``null``/omitted if not needed
    (e.g. simulated SLMs). """

@dataclass(frozen=True)
class FocusLockInfo:
    camera: str
    """ Detector name. """

    positioner: str
    """ Positioner name. """

    updateFreq: int
    """ Update frequency, in milliseconds. """

    frameCropx: int
    """ Starting X position of camera frame crop. """

    frameCropy: int
    """ Starting Y position of camera frame crop. """

    frameCropw: int
    """ Width of camera frame crop. """

    frameCroph: int
    """ Height of camera frame crop. """

    piKp: float
    """ Default kp value of feedback loop. """

    piKi: float
    """ Default ki value of feedback loop. """

    swapImageAxes: bool = False
    """ Swap camera image axes when grabbing camera frame. """

    positionerAxis: Optional[Union[str, int]] = None
    """ Positioner axis used for focus-lock movements. Defaults to ``"Z"`` if
    available on the configured positioner, otherwise ``0``. """

    reacquireTimeoutS: float = 1.0
    """ How long to wait for the focus signal to come back after a scan
    released the actuator, before giving up, in seconds.

    The lock is suspended for the duration of any scan that can reach its axis
    and does not resume the instant the scan ends -- it first waits for the
    signal to settle *and* to return near the setpoint it was holding. On
    timeout the lock is left off and a warning is logged, rather than
    re-engaging against a signal that never came back.

    At the default ``updateFreq`` this is only a handful of estimates, so raise
    it on rigs whose piezo takes longer to settle than the camera takes to
    deliver ``reacquireSamples`` frames. """

    reacquireTolerancePx: float = 0.5
    """ How close the focus signal must return to its pre-scan setpoint before
    the lock re-engages, in camera pixels.

    PLACEHOLDER DEFAULT -- chosen to match ``aboutToLockDiffMax`` and not
    measured on hardware. The meaningful value depends on the rig's px-to-µm
    calibration; too loose re-engages against a defocused sample, too tight
    times out on every tile. Calibrate this before relying on 3D tiling. """

    reacquireSamples: int = 5
    """ Number of consecutive focus estimates the reacquisition barrier
    averages before deciding the signal has settled. """

@dataclass(frozen=True)
class AutofocusInfo:
    camera: str
    """ Detector name. """

    positioner: str
    """ Positioner name. """

    updateFreq: int
    """ Update frequency, in milliseconds. """

    frameCropx: int
    """ Starting X position of frame crop. """

    frameCropy: int
    """ Starting Y position of frame crop. """

    frameCropw: int
    """ Width of frame crop. """

    frameCroph: int
    """ Height of frame crop. """


@dataclass(frozen=True)
class TilingInfo:
    xyPositioner: str
    """ Name of the XY positioner (must match a positioner in the setup). """

    zPositioner: str = ""
    """ Name of the Z positioner used for per-tile autofocus. Empty = disabled. """

    camera: str = ""
    """ Detector to use for tile acquisition. Empty = first forAcquisition detector. """

    defaultTileStepUm: float = 100.0
    """ Default stage step between tile centres, in µm. """

    settleTimeMs: float = 150.0
    """ Time to wait after each stage move before acquiring a tile, in ms.

    Covers mechanical settling of the stage. Too short and tiles are captured
    while the stage is still ringing, which shows up as a mosaic that will not
    overlap cleanly. The widget can override this per run. """

    mode: str = "free-running"
    """ Default acquisition timing model: ``free-running`` (grab a frame from a
    continuously running camera) or ``triggered`` (run one scan per tile).

    Triggered mode covers both scanned detectors, which build their image as
    the scan runs, and a camera wired to the scan's trigger output. """

    scanSource: str = ""
    """ Widget key of the scan controller to trigger in ``triggered`` mode.
    Empty resolves automatically, which is unambiguous unless the setup has
    several scan controllers. """

    scanTimeoutS: float = 300.0
    """ How long to wait for one tile's scan to finish before giving up. """

    saveTiles: bool = False
    """ Save every tile, plus the stitched mosaic and stitching sidecars, as
    each run proceeds.

    Tiles are written through the ordinary recording/storer layer, so each one
    is an OME image carrying its own stage position in ``Plane/@PositionX|Y``.
    A ``TileConfiguration.txt`` (Fiji Grid/Collection Stitching, BigStitcher)
    and a ``tiles.json`` manifest are written alongside them. """

    saveFormat: str = "TIFF"
    """ Format for saved tiles: ``TIFF`` (OME-TIFF), ``HDF5`` or ``ZARR``
    (OME-NGFF). OME-TIFF is the default because it is what stitching tools read
    natively. """

    measurementsRoot: str = ""
    """ Base folder for saved tiling datasets. Empty uses the ImSwitch default
    measurements root. Each run gets its own ``<date>/tiling_<time>`` folder. """

    flipTileAxisX: bool = False
    """ Mirror the mosaic along X when assembling it.

    The stitcher assumes a positive stage X move places the next tile further
    right in the overview. Whether that holds depends on how the camera is
    mounted and on the stage's sign convention, so set this when the mosaic
    builds left/right opposite to the physical movement. Affects only how tiles
    are assembled and how a click maps back to a stage position — the stage
    itself traces the same physical spiral either way. """

    flipTileAxisY: bool = False
    """ Mirror the mosaic along Y when assembling it. See ``flipTileAxisX``. """

    swapTileAxes: bool = False
    """ Exchange the mosaic's X and Y axes, for a camera mounted at 90 degrees
    to the stage axes. Applied before ``flipTileAxisX``/``flipTileAxisY``. """

    registerTiles: bool = False
    """ Refine each tile's placement by phase correlation against its already-
    placed neighbours, instead of trusting the commanded stage position alone.

    Useful when the stage is not repeatable enough for seamless stitching. The
    measured corrections are also reported after each run, which distinguishes
    stage error from a wrong sample-plane pixel size. """

    registrationMaxShiftFraction: float = 0.5
    """ Reject registration corrections larger than this fraction of the tile
    step. Guards against false matches on periodic sample structure. """


@dataclass(frozen=True)
class ScanInfo:
    scanWidgetType: str
    """ Type of scan widget to generate: PointScan/MoNaLISA/Base/etc."""

    scanDesigner: str
    """ Name of the scan designer class to use. """

    scanDesignerParams: Dict[str, Any]
    """ Params to be read by the scan designer. """

    TTLCycleDesigner: str
    """ Name of the TTL cycle designer class to use. """

    TTLCycleDesignerParams: Dict[str, Any]
    """ Params to be read by the TTL cycle designer. """

    sampleRate: int
    """ Scan sample rate. """

    maxScanTimeMin: Optional[int] = None
    """ Max scan time allowed, in min. ``null``/omitted = no limit. """

    lineClockLine: Optional[Union[str, int]] = None
    """ Line for line clock output. ``null`` if not wanted or NI-DAQ is not used.
    If integer, it will be translated to "Dev1/port0/line{lineClockLine}".
    """

    frameStartClockLine: Optional[Union[str, int]] = None
    """ Line for frame startclock output. ``null`` if not wanted or NI-DAQ is not used.
    If integer, it will be translated to "Dev1/port0/line{frameStartClockLine}".
    """

    frameEndClockLine: Optional[Union[str, int]] = None
    """ Line for frame end clock output. ``null`` if not wanted or NI-DAQ is not used.
    If integer, it will be translated to "Dev1/port0/line{frameEndClockLine}".
    """



@dataclass(frozen=True)
class EtSTEDDeviceInfo:
    detectorFast: str
    """ Name of the STED detector to use. """ #comment from Simone, should this be "widefield detector to use?"

    detectorSlow: str
    """ Name of the widefield detector to use. """ #comment from Simone, should this be "STED detector to use?"

    laserFast: str
    """ Name of the widefield laser to use. """


@dataclass(frozen=True)
class MicroscopeStandInfo:
    managerName: str
    """ Name of the manager to use. """

    rs232device: str
    """ Name of the rs232 device to use. """

    managerProperties: Optional[dict]
    """ Dict with microscope stand-specific info such as available cubes"""


@dataclass(frozen=True)
class EtSTEDInfo:
    swapXY: bool = False
    """ Swap X and Y axes before transforming coordinates of a detected event. """

    invertX: bool = False
    """ Invert X value before transforming coordinates of a detected event. """
    
    invertY: bool = False
    """ Invert Y value before transforming coordinates of a detected event. """


@dataclass(frozen=True)
class NidaqInfo:
    timerCounterChannel: Optional[Union[str, int]] = None
    """ Output for Counter for timing purposes. If an integer is specified, it
    will be translated to "Dev1/ctr{timerCounterChannel}". """

    startTrigger: bool = False
    """ Boolean for start triggering for sync. """

    simulation: Optional[bool] = False
    """ Boolean for allowing to run nidaq-commands without access to a nidaq card. """

    def getTimerCounterChannel(self):
        """ :meta private: """
        if isinstance(self.timerCounterChannel, int):
            return f'Dev1/ctr{self.timerCounterChannel}'  # for backwards compatibility
        else:
            return self.timerCounterChannel


@dataclass(frozen=True)
class PulseStreamerInfo:
    ipAddress: Optional[str] = None
    """ IP address of Pulse Streamer hardware. """


@dataclass(frozen=True)
class TeensyPulseInfo:
    """Config for a Teensy / Arduino pulse generator running the
    ImSwitch v4 firmware (with v3 fallback).

    See ``imswitch.imcontrol.model.managers.pulsegen.TeensyPulseManager``
    for the consumer.  Leave ``port`` as ``None`` to skip construction
    entirely; the manager will not be instantiated.  Provide a port and
    set ``useMockOnFailure=True`` to get an in-process mock when the
    real device is unavailable.
    """
    port: Optional[str] = None
    """ Serial port, e.g. ``COM7`` or ``/dev/ttyACM0``.  ``None`` =
    pulse generator disabled. """

    baud: int = 115200

    pinMap: Dict[str, int] = field(default_factory=dict)
    """ Optional name → channel mapping.  Not enforced by the manager;
    purely a convenience for scripts to refer to ``pinMap['laser488']``
    rather than hardcoded integers. """

    useMockOnFailure: bool = True
    """ Fall back to the in-process mock if the port can't be opened.
    Set False to make a missing device a hard failure. """

    mockNChannels: int = 16
    mockMinPulseUs: int = 1
    mockMaxSteps: int = 256
    """ Capabilities the mock advertises when the real device is
    unavailable.  Ignored when a real connection succeeds (firmware
    reports its own via ``*IDN?``). """


@dataclass(frozen=True)
class PyroServerInfo:
    name: Optional[str] = 'ImSwitchServer'
    host: Optional[str] = '127.0.0.1'
    port: Optional[int] = 54333
    active: Optional[bool] = False


@dataclass(frozen=True)
class TriggerScopeInfo:
    rs232device: str
    """ Name of the RS232 device key (in rs232devices) that connects to the
    TriggerScope board over serial. """


@dataclass(frozen=True)
class FlipMirrorInfo:
    managerName: str
    """ Flip mirror manager class name. """

    serial_number: Optional[str] = None
    """ Serial number used by hardware managers to find the device. """

    invert: bool = False
    """ Whether logical states 0 and 1 are swapped from hardware states. """

    initial_state: Optional[int] = None
    """ Optional state to move to at startup. ``None`` keeps the current state. """

    state_names: Dict[str, str] = field(default_factory=dict)
    """ Optional display names for logical states 0 and 1. """

    managerProperties: Dict[str, Any] = field(default_factory=dict)
    """ Optional manager-specific properties. """


@dataclass_json(undefined=Undefined.INCLUDE)
@dataclass
class SetupInfo:
    # default_factory seems to be required for the field to show up in autodocs for deriving classes

    detectors: Dict[str, DetectorInfo] = field(default_factory=dict)
    """ Detectors in this setup. This is a map from unique detector names to
    DetectorInfo objects. """

    lasers: Dict[str, LaserInfo] = field(default_factory=dict)
    """ Lasers in this setup. This is a map from unique laser names to
    LaserInfo objects. """

    positioners: Dict[str, PositionerInfo] = field(default_factory=dict)
    """ Positioners in this setup. This is a map from unique positioner names
    to DetectorInfo objects. """

    rs232devices: Dict[str, RS232Info] = field(default_factory=dict)
    """ RS232 connections in this setup. This is a map from unique RS232
    connection names to RS232Info objects. Some detector/laser/positioner
    managers will require a corresponding RS232 connection to be referenced in
    their properties.
    """

    slm: Optional[SLMInfo] = field(default_factory=lambda: None)
    """ SLM settings. Required to be defined to use SLM functionality. """

    slms: Dict[str, SLMsInfo] = field(default_factory=dict)
    """ SLM settings. Required to be defined to use SLM functionality. """

    focusLock: Optional[FocusLockInfo] = field(default_factory=lambda: None)
    """ Focus lock settings. Required to be defined to use focus lock
    functionality. """
    
    autofocus: Optional[AutofocusInfo] = field(default_factory=lambda: None)
    """ Autofocus settings. Required to be defined to use autofocus 
    functionality. """

    tiling: Optional[TilingInfo] = field(default_factory=lambda: None)
    """ Tiling scan settings. None = tiling disabled. """

    scan: Optional[ScanInfo] = field(default_factory=lambda: None)
    """ Scan settings. Required to be defined to use scan functionality. """

    rotators: Optional[Dict[str, DeviceInfo]] = field(default_factory=lambda: None)
    """ Standa motorized rotator mounts settings. Required to be defined to use rotator functionality. """

    flipMirrors: Optional[Dict[str, FlipMirrorInfo]] = field(default_factory=lambda: None)
    """ Motorized flip mirror settings. """

    microscopeStand: Optional[MicroscopeStandInfo] = field(default_factory=lambda: None)
    """ Microscope stand settings. Required to be defined to use MotCorr widget. """

    etSTED: Optional[EtSTEDInfo] = field(default_factory=lambda: None)
    """ EtSTED stand settings. """

    nidaq: NidaqInfo = field(default_factory=NidaqInfo)
    """ NI-DAQ settings. """

    pulseStreamer: PulseStreamerInfo = field(default_factory=PulseStreamerInfo)
    """ Pulse Streamer settings. """

    teensyPulse: Optional[TeensyPulseInfo] = field(default_factory=lambda: None)
    """ Teensy / Arduino pulse generator settings.  ``None`` = no
    Teensy in this setup.  See :class:`TeensyPulseInfo`. """

    pyroServerInfo: PyroServerInfo = field(default_factory=PyroServerInfo)

    triggerScope: Optional[TriggerScopeInfo] = field(default_factory=lambda: None)
    """ TriggerScope DAQ board settings. Required to use TriggerScope hardware. """

    shortcuts: Optional[Dict[str, Union[str, List[str], None]]] = field(default_factory=lambda: None)
    """ Keyboard shortcut configuration. Maps action IDs to key sequences.
    Each value can be a single string (e.g., "Ctrl+R"), a list of strings for
    multiple sequences, or null to explicitly disable a default binding. """

    smartMicroscopyModes: Optional[Dict[str, Dict[str, str]]] = field(
        default_factory=lambda: None
    )
    """ Smart microscopy mode-switching configuration for event-triggered
    workflows. Maps ``workflowName -> {role: setupModeName}``, where each role
    names an existing setup mode to apply for that runtime phase. Recognized
    roles are ``scouting``, ``event``, ``resume``, ``idle``, and ``validation``.
    ``None`` (the default) or an absent key means no smart-mode mapping is
    configured; setups without this section still parse. """

    smartMicroscopyModePolicies: Optional[Dict[str, str]] = field(
        default_factory=lambda: None
    )
    """ Per-workflow non-interactive hazard policy for smart microscopy mode
    switching. Maps ``workflowName -> policyName``, where ``policyName`` is one
    of ``allow``, ``warnOnly``, or ``blockOnHazard``. Workflows that are absent
    (or name an unknown policy) default to ``blockOnHazard`` -- the safest
    choice, blocking arming when a preflight finds hazards or missing role modes.
    ``None`` (the default) or an absent key means every workflow uses the
    ``blockOnHazard`` default; setups without this section still parse. """

    smartMicroscopyModeSwitchingEnabled: Optional[Dict[str, bool]] = field(
        default_factory=lambda: None
    )
    """ Per-workflow rollout flag for replacing legacy workflow-specific mode
    switching with ``SmartMicroscopyModeService``. Maps ``workflowName -> bool``.
    ``False`` or an absent workflow keeps the legacy path so labs can opt in and
    roll back without changing code. """

    _catchAll: CatchAll = None

    def getDevice(self, deviceName):
        """ Returns the DeviceInfo for a specific device.

        :meta private:
        """
        return self.getAllDevices()[deviceName]

    def getTTLDevices(self):
        """ Returns DeviceInfo from all devices that have a digitalLine.

        :meta private:
        """
        devices = {}
        i = 0
        for deviceInfos in self.lasers, self.detectors:
            deviceInfosCopy = deviceInfos.copy()
            for item in list(deviceInfosCopy):
                if deviceInfosCopy[item].getDigitalLine() is None:
                    del deviceInfosCopy[item]
            devices.update(deviceInfosCopy)
            i += 1

        return devices

    def getDetectors(self):
        """ :meta private: """
        devices = {}
        for deviceInfos in self.detectors:
            devices.update(deviceInfos)

        return devices

    def getAllDevices(self):
        """ :meta private: """
        devices = {}
        for deviceInfos in self.lasers, self.detectors, self.positioners:
            devices.update(deviceInfos)

        return devices


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
