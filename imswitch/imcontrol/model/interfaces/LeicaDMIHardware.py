import importlib
import threading
import weakref


_PRIVATE_MODULE_NAME = "imswitch.imcontrol.model.interfaces.LeicaDMIHardware_private"
_PRIVATE_CLASS_NAME = "RealLeicaDMIHardware"
_CACHE_LOCK = threading.RLock()
_HARDWARE_CACHE = weakref.WeakKeyDictionary()


def createLeicaDMIHardware(rs232Manager, *, managerProperties=None, logger=None):
    """Create or return the shared Leica DMI hardware interface.

    Returns ``None`` when the private implementation is unavailable or when the
    hardware cannot be reached.
    """
    if rs232Manager is None:
        _log(logger, "error", "Leica DMI hardware unavailable: no RS232 manager was provided.")
        return None

    privateClass = _loadPrivateHardwareClass(logger)
    if privateClass is None:
        return None

    managerProperties = managerProperties or {}

    with _CACHE_LOCK:
        hardware = _HARDWARE_CACHE.get(rs232Manager)
        if hardware is not None:
            _configureHardware(hardware, managerProperties, logger)
            if _isConnected(hardware):
                return hardware

            _log(
                logger,
                "warning",
                "Leica DMI hardware unavailable: cached private interface is not connected.",
            )
            return None

        try:
            hardware = privateClass(
                rs232Manager,
                managerProperties=managerProperties,
                logger=logger,
            )
        except Exception as exc:
            _log(
                logger,
                "error",
                "Leica DMI hardware unavailable: private interface loaded, "
                f"but connection failed: {exc}",
            )
            return None

        if not _isConnected(hardware):
            _log(
                logger,
                "error",
                "Leica DMI hardware unavailable: private interface loaded, "
                "but connection probe failed.",
            )
            return None

        _HARDWARE_CACHE[rs232Manager] = hardware
        return hardware


def reconnectLeicaDMIHardware(
    rs232Manager, *, managerPropertiesSequence=None, logger=None
):
    """Revalidate or create the shared Leica interface after RS232 reconnect.

    Existing hardware-interface identity is preserved when one was already
    created. This matters for the stand and objective-Z managers, which share
    the same object. When startup never produced an interface (for example
    because RS232 fell back to mock), a new shared interface is created.
    """
    configurations = list(managerPropertiesSequence or [{}])
    if not configurations:
        configurations = [{}]

    with _CACHE_LOCK:
        hardware = _HARDWARE_CACHE.get(rs232Manager)

        if hardware is None:
            hardware = createLeicaDMIHardware(
                rs232Manager,
                managerProperties=configurations[0],
                logger=logger,
            )
            remaining = configurations[1:]
        else:
            reconnect = getattr(hardware, "reconnect", None)
            if callable(reconnect):
                try:
                    reconnect()
                except Exception as exc:
                    _log(
                        logger,
                        "error",
                        f"Leica DMI reconnect probe failed: {exc}",
                    )
                    return None
            elif not _isConnected(hardware):
                _log(
                    logger,
                    "error",
                    "Leica DMI reconnect unavailable: cached interface is disconnected.",
                )
                return None
            remaining = configurations

        if hardware is None or not _isConnected(hardware):
            return None

        for config in remaining:
            _configureHardware(hardware, config, logger)
        return hardware


def _loadPrivateHardwareClass(logger):
    try:
        module = importlib.import_module(_PRIVATE_MODULE_NAME)
    except ModuleNotFoundError as exc:
        if exc.name == _PRIVATE_MODULE_NAME:
            _log(
                logger,
                "error",
                "Leica DMI hardware unavailable: private interface file "
                "LeicaDMIHardware_private.py is not installed.",
            )
            return None
        _log(
            logger,
            "error",
            "Leica DMI hardware unavailable: private interface dependency "
            f"could not be imported: {exc}",
        )
        return None
    except Exception as exc:
        _log(
            logger,
            "error",
            f"Leica DMI hardware unavailable: private interface import failed: {exc}",
        )
        return None

    try:
        return getattr(module, _PRIVATE_CLASS_NAME)
    except AttributeError:
        _log(
            logger,
            "error",
            "Leica DMI hardware unavailable: private interface file does not "
            f"define {_PRIVATE_CLASS_NAME}.",
        )
        return None


def _configureHardware(hardware, managerProperties, logger):
    configure = getattr(hardware, "configure", None)
    if configure is None:
        return

    try:
        configure(managerProperties or {})
    except Exception as exc:
        _log(logger, "warning", f"Leica DMI hardware configuration failed: {exc}")


def _isConnected(hardware):
    isConnected = getattr(hardware, "isConnected", None)
    if isConnected is None:
        return True
    return bool(isConnected())


def _log(logger, level, message):
    if logger is None:
        return

    logMethod = getattr(logger, level, None)
    if logMethod is not None:
        logMethod(message)
