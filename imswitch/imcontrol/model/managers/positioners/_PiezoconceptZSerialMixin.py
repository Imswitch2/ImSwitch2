import threading


class PiezoconceptZSerialMixin:
    """Serialize and validate Piezoconcept command/reply transactions."""

    _MAX_REPLY_LINES = 3

    def _initPiezoconceptSerial(self, logger):
        self._piezoconceptSerialLock = threading.RLock()
        self._piezoconceptLogger = logger

    @staticmethod
    def _cleanPiezoconceptReply(reply):
        if isinstance(reply, bytes):
            reply = reply.decode('ascii', errors='replace')
        return str(reply).replace('\x00', '').strip()

    @classmethod
    def _positionFromPiezoconceptReply(cls, reply):
        if reply is None:
            return None

        cleanedReply = cls._cleanPiezoconceptReply(reply)
        if not cleanedReply:
            return None

        try:
            return float(cleanedReply.split()[0])
        except (ValueError, IndexError):
            return None

    def _readNextPiezoconceptReply(self):
        read = getattr(self._rs232Manager, 'read', None)
        if read is None:
            return None
        try:
            return read()
        except Exception as e:
            self._piezoconceptLogger.warning(
                f'Could not read the next Piezoconcept reply: {e}'
            )
            return None

    def _queryPiezoconceptMove(self, command):
        """Send a movement command and consume its ``Ok`` acknowledgement."""
        with self._piezoconceptSerialLock:
            replies = []
            reply = self._rs232Manager.query(command)

            for _ in range(self._MAX_REPLY_LINES):
                replies.append(reply)
                if reply is None:
                    if len(replies) == 1:
                        return
                    break

                cleanedReply = self._cleanPiezoconceptReply(reply)
                if cleanedReply.casefold() == 'ok':
                    return

                reply = self._readNextPiezoconceptReply()

            self._piezoconceptLogger.warning(
                f'Piezoconcept move command {command!r} returned unexpected '
                f'replies: {replies!r}'
            )

    def _queryPiezoconceptPosition(self, command):
        """Return a numeric position, skipping a delayed ``Ok`` if necessary."""
        with self._piezoconceptSerialLock:
            replies = []
            reply = self._rs232Manager.query(command)

            for _ in range(self._MAX_REPLY_LINES):
                replies.append(reply)
                if reply is None:
                    if len(replies) == 1:
                        return None
                    break

                position = self._positionFromPiezoconceptReply(reply)
                if position is not None:
                    return position

                cleanedReply = self._cleanPiezoconceptReply(reply)
                if (
                    not cleanedReply
                    or cleanedReply.casefold() == 'ok'
                    or cleanedReply.casefold() == command.casefold()
                ):
                    reply = self._readNextPiezoconceptReply()
                    continue

                break

            self._piezoconceptLogger.warning(
                f'Piezoconcept position command {command!r} returned no numeric '
                f'position: {replies!r}'
            )
            return None
