# Eager re-exports of hardware-specific modules.
#
# These imports are load-bearing for some workflows: importing them at
# package-load time pre-initialises COM and pulls in vendor USB/RS232 DLLs
# that downstream code (notably DCAM camera open) implicitly depends on.
# Removing them caused the Hamamatsu camera to hang on dcam_open / dcamdev_open
# in fresh Python environments and in any rebuilt environment where these
# side effects didn't run first. Each import is guarded so the package
# still loads in environments without the optional hardware extras.
try:
    from .hamamatsu import HamamatsuCamera, HamamatsuCameraMR
except Exception:
    pass
try:
    from .hamamatsu_mock import MockHamamatsu
except Exception:
    pass
try:
    from .lantzlasers import LantzLaser
except Exception:
    pass
try:
    from .standamotor import StandaMotor, MockStandaMotor
except Exception:
    pass
