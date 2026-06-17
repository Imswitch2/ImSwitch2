from .Options import Options
from .SetupInfo import (
    DeviceInfo,
    DetectorInfo,
    FlipMirrorInfo,
    LaserInfo,
    PositionerInfo,
    ScanInfo,
    SetupInfo,
)
from .WidgetStatePersistence import WidgetStatePersistence, getWidgetStatePersistence
from .errors import *
from .managers import *
from .signaldesigners import SignalDesignerFactory
import sys

#sys.modules['visa'] = 'pyvisa'
