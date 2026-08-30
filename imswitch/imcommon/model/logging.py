import inspect
import logging
import weakref

import coloredlogs

LEVEL_STYLES = {
    'debug': {'color': 'cyan', 'bold': True},
    'info': {'color': 'blue'},
    'warning': {'color': 'yellow'},
    'error': {'color': 'red'},
    'critical': {'color': 'red', 'bold': True},
}

baseLogger = logging.getLogger('imswitch')
baseLogger.propagate = False # avoid propagation to python root logger

# Third-party loggers
_externalLoggers = {
    'slmcore': logging.getLogger('slmcore'),
}

# Default to INFO. Pass `--debug` on the imswitch CLI (or set the env var
# `IMSWITCH_LOG_LEVEL=DEBUG`) to see debug-level messages from every manager.
import os as _os
_default_level = _os.environ.get('IMSWITCH_LOG_LEVEL', 'INFO').upper()

def _configureLoggers(level):
    coloredlogs.install(
        level=level,
        logger=baseLogger,
        level_styles=LEVEL_STYLES,
        fmt='%(asctime)s %(levelname)s %(message)s',
    )

    for logger in _externalLoggers.values():
        coloredlogs.install(
            level=level,
            logger=logger,
            level_styles=LEVEL_STYLES,
            fmt='%(asctime)s %(levelname)s [%(name)s] %(message)s',
        )

        # External thirdparty descendants propagate to their own logger.
        # Stop there so they don't reach the root logger and get printed twice.
        logger.propagate = False


_configureLoggers(_default_level)


def setLogLevel(level):
    """Override the imswitch logger level at runtime.

    ``level`` may be a string (``'DEBUG'``, ``'INFO'``, ...) or an int.
    """
    _configureLoggers(level)
    coloredlogs.install(level=level, logger=baseLogger, level_styles=LEVEL_STYLES,
                        fmt='%(asctime)s %(levelname)s %(message)s')


objLoggers = {}


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger, prefixes, objRef):
        super().__init__(logger, {})
        self.prefixes = prefixes
        self.objRef = objRef

    def process(self, msg, kwargs):
        processedPrefixes = []
        for prefix in self.prefixes:
            if callable(prefix):
                try:
                    processedPrefixes.append(prefix(self.objRef()))
                except Exception:
                    pass
            else:
                processedPrefixes.append(prefix)

        processedMsg = f'[{" -> ".join(processedPrefixes)}] {msg}'
        return processedMsg, kwargs


def initLogger(obj, *, instanceName=None, tryInheritParent=False):
    """ Initializes a logger for the specified object. obj should be either a
    class, object or string. """

    logger = None

    if tryInheritParent:
        # Use logger from first parent in stack that has one
        for frameInfo in inspect.stack():
            frameLocals = frameInfo[0].f_locals
            if 'self' not in frameLocals:
                continue

            parent = frameLocals['self']
            try:
                parentRef = weakref.ref(parent)
            except TypeError:
                # Some objects (e.g., pytest HookCaller) don't support weak references
                continue
            if parentRef not in objLoggers:
                continue

            logger = objLoggers[parentRef]
            break

    if logger is None:
        # Create logger
        if inspect.isclass(obj):
            objName = obj.__name__
            objRef = weakref.ref(obj)
        elif isinstance(obj, str):
            objName = obj
            objRef = None
        else:
            objName = obj.__class__.__name__
            objRef = weakref.ref(obj)

        logger = LoggerAdapter(baseLogger,
                               [objName, instanceName] if instanceName else [objName],
                               objRef)

        # Save logger so it can be used by tryInheritParent requesters later
        if objRef is not None:
            objLoggers[objRef] = logger

    return logger
