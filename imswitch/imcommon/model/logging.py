import collections
import inspect
import logging
import logging.handlers
import os
import sys
import weakref

import coloredlogs

LEVEL_STYLES = {
    'debug': {'color': 'cyan', 'bold': True},
    'info': {'color': 'blue'},
    'warning': {'color': 'yellow'},
    'error': {'color': 'red'},
    'critical': {'color': 'red', 'bold': True},
}

LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'

baseLogger = logging.getLogger('imswitch')

#: Records kept in memory for the Log panel.  At ~200 bytes of formatted text
#: each this is a few MB in the worst case, which buys roughly a full session of
#: a chatty DEBUG run.
LOG_BUFFER_CAPACITY = 5000


class LogRecordBuffer(logging.Handler):
    """ The last :data:`LOG_BUFFER_CAPACITY` records, plus live fan-out.

    Two jobs, and the first is the reason this is a handler rather than
    something the Log panel owns:

    * **History.**  The panel can be opened at any point in a session, and the
      interesting records are usually the ones from before the user thought to
      open it.  Buffering starts when this module is imported -- earlier than
      any window exists -- so opening the panel shows what already happened.
    * **Live updates.**  Listeners registered with :meth:`addListener` are
      called for every subsequent record.

    Records are buffered at DEBUG whatever the console is set to, so detail is
    available without restarting with ``--debug``.  On a microscope that matters:
    the run that misbehaved is often not one you can repeat.

    Thread-safe for appending -- ``logging.Handler.handle`` takes the handler
    lock, and a ``deque`` with a ``maxlen`` is atomic for append.  Listeners are
    called on whichever thread logged, so a GUI listener must marshal to the GUI
    thread itself (:class:`~imswitch.imcommon.view.LogWidget.LogWidget` does).
    """

    def __init__(self, capacity=LOG_BUFFER_CAPACITY):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter(LOG_FORMAT))
        self._records = collections.deque(maxlen=capacity)
        self._listeners = []
        #: Listener failures seen so far; the first one is reported to stderr.
        self._listenerErrors = 0

    def emit(self, record):
        try:
            entry = (record.levelno, record.levelname, self.format(record))
        except Exception:  # pragma: no cover - a broken format string
            self.handleError(record)
            return
        self._records.append(entry)
        for listener in list(self._listeners):
            try:
                listener(entry)
            except Exception as err:
                # A listener that raises must not break logging for everyone
                # else, and must not recurse into logging to complain -- but it
                # must not vanish either.  Swallowing this silently is what hid
                # a Log panel whose live updates never worked at all: the panel
                # still backfilled from the buffer, so it looked right.
                if not self._listenerErrors:
                    print(f'imswitch: a log listener raised {err!r}; live log'
                          f' updates may be incomplete', file=sys.stderr)
                self._listenerErrors += 1

    def records(self):
        """ The buffered records, oldest first, as ``(levelno, levelname, text)``. """
        return list(self._records)

    def addListener(self, listener):
        """ Call ``listener((levelno, levelname, text))`` for every new record. """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def removeListener(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def clear(self):
        self._records.clear()


#: The process-wide buffer.  Attached below, before anything else can log.
logBuffer = LogRecordBuffer()


def _consoleHandlers():
    """ The stream handlers coloredlogs installed -- and nothing else.

    Identified by type rather than by excluding the ones we know about: an
    exclusion list quietly swept up the log *file* handler too, so changing the
    console level clamped the file to the same level and the file stopped
    recording the DEBUG detail it exists to keep.  ``FileHandler`` subclasses
    ``StreamHandler``, hence the explicit exclusion. """

    return [h for h in baseLogger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, (logging.FileHandler, LogRecordBuffer))]


def _install(consoleLevel):
    """ (Re)install the console handler at ``consoleLevel`` and keep buffering DEBUG.

    ``coloredlogs.install`` sets the level on the *logger*, which would stop
    DEBUG records ever reaching the buffer.  So the logger is opened up to DEBUG
    afterwards and the level is put back on the console handler, where it
    belongs: the console shows what it was asked for, the buffer sees everything.
    """
    coloredlogs.install(level=consoleLevel, logger=baseLogger, level_styles=LEVEL_STYLES,
                        fmt=LOG_FORMAT)
    for handler in _consoleHandlers():
        handler.setLevel(consoleLevel)
    baseLogger.setLevel(logging.DEBUG)
    # coloredlogs.install() replaces its own handler; re-adding ours is cheap
    # insurance against it clearing the list in some future version.
    if logBuffer not in baseLogger.handlers:
        baseLogger.addHandler(logBuffer)


# Default to INFO. Pass `--debug` on the imswitch CLI (or set the env var
# `IMSWITCH_LOG_LEVEL=DEBUG`) to see debug-level messages from every manager.
_default_level = os.environ.get('IMSWITCH_LOG_LEVEL', 'INFO').upper()
_install(_default_level)


def setLogLevel(level):
    """Override the *console* log level at runtime.

    ``level`` may be a string (``'DEBUG'``, ``'INFO'``, ...) or an int.  The
    in-memory buffer and the log file keep recording at DEBUG regardless; the
    Log panel has its own filter.
    """
    _install(level)


def attachLogFile(path, maxBytes=5 * 1024 * 1024, backupCount=3):
    """ Also write the log to ``path``, starting with what is already buffered.

    A panel can only show a session that got far enough to open one.  The file
    is what is left when a bundle dies during startup, and the thing to ask a
    user to send -- which is why the buffered backlog is written into it rather
    than lost.

    Returns the handler, or None if the file could not be opened (a read-only
    or full disk must not stop ImSwitch2 from starting).
    """
    global _logFileHandler

    if _logFileHandler is not None:
        return _logFileHandler

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=maxBytes, backupCount=backupCount, encoding='utf-8'
        )
    except OSError as err:
        baseLogger.warning(f'Could not open the log file {path}: {err}')
        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    # The backlog is already formatted, so it goes to the stream verbatim rather
    # than through the handler -- emitting it as records would either re-stamp
    # every line with the time it was replayed, or force a '%(message)s'
    # formatter on the handler and strip the timestamp off every *live* record
    # after it.
    backlog = logBuffer.records()
    if backlog:
        try:
            handler.stream.write(
                ''.join(text + handler.terminator for _levelno, _levelname, text in backlog)
            )
            handler.flush()
        except Exception as err:  # pragma: no cover
            baseLogger.warning(f'Could not write the buffered log backlog: {err}')

    baseLogger.addHandler(handler)
    _logFileHandler = handler
    return handler


def logFilePath():
    """ Where :func:`attachLogFile` is writing, or None. """
    return getattr(_logFileHandler, 'baseFilename', None)


_logFileHandler = None


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
