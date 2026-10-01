import threading

import numpy as np
from scipy.interpolate import interp1d


class RealLeicaDMIHardware:
    def __init__(self, rs232Manager, *, managerProperties=None, logger=None):
        self.__logger = logger
        self._rs232Manager = rs232Manager
        self._lock = threading.RLock()
        self._is_connected = False
        self._connection_error = None

        self._lut_du_to_nm = None
        self._lut_nm_to_du = None
        self._z_um_per_count = None
        self._value_units = "arb"
        self._position = 0
        self._z_lower_limit_du = 0
        self._z_upper_limit_du = None

        self.configure(managerProperties or {})
        self._probe_connection()
        self._load_z_limits()

        current_position = self.get_z_position_device_units()
        if current_position is not None:
            self._position = current_position

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def configure(self, managerProperties):
        try:
            calib_csv_path = managerProperties["calibCsvPath"]
        except (TypeError, KeyError):
            if self._z_um_per_count is None:
                self._load_z_conversion_factor()
            return

        try:
            self.create_lut_from_calib(calib_csv_path)
            self._value_units = "um"
            return
        except Exception as exc:
            if self.__logger is not None:
                self.__logger.warning(
                    "Could not load Leica DMI Z calibration LUT from "
                    f"{calib_csv_path}: {exc}"
                )

        if self._z_um_per_count is None:
            self._load_z_conversion_factor()

    def _load_z_conversion_factor(self):
        try:
            conversion_factor = self._query_float("71042")
        except Exception as exc:
            if self.__logger is not None:
                self.__logger.warning(
                    "Could not read Leica DMI Z conversion factor with 71042: "
                    f"{exc}"
                )
            return

        if conversion_factor is None or conversion_factor <= 0:
            if self.__logger is not None:
                self.__logger.warning(
                    "Leica DMI Z conversion factor is unavailable or invalid."
                )
            return

        self._z_um_per_count = conversion_factor
        self._value_units = "um"

    def _load_z_limits(self):
        self._z_upper_limit_du = self.get_z_upper_limit_device_units(refresh=True)

    def _probe_connection(self):
        try:
            reply = self._query("71023")
        except Exception as exc:
            self._mark_disconnected_on_error(exc)
            raise RuntimeError(f"connection probe failed: {exc}") from exc

        if reply is None:
            self._is_connected = False
            raise RuntimeError("connection probe returned no reply")

        self._is_connected = True

    def isConnected(self):
        return self._is_connected

    @property
    def connectionError(self):
        return self._connection_error

    def _mark_disconnected_on_error(self, exception):
        was_connected = self._is_connected
        self._is_connected = False
        self._connection_error = str(exception)

        if was_connected and self.__logger is not None:
            self.__logger.warning(f"Leica DMI communication failed: {exception}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(self, cmd):
        with self._lock:
            try:
                reply = self._rs232Manager.query(cmd)
            except Exception as exc:
                self._mark_disconnected_on_error(exc)
                raise

        if reply is None:
            self._mark_disconnected_on_error(f"No reply for Leica DMI command {cmd}")
            return None

        self._is_connected = True
        self._connection_error = None
        return reply

    def _write(self, cmd):
        with self._lock:
            try:
                result = self._rs232Manager.write(cmd)
            except Exception as exc:
                self._mark_disconnected_on_error(exc)
                raise

        self._is_connected = True
        self._connection_error = None
        return result

    def _strip_reply_prefix(self, reply, cmd=None):
        if reply is None:
            return reply

        reply = str(reply).strip()

        if cmd is not None:
            cmd = str(cmd).strip()
            if reply.startswith(cmd + " "):
                return reply[len(cmd) + 1:]
            if reply.startswith("$" + cmd + " "):
                return reply[len(cmd) + 2:]

        if len(reply) > 6 and reply[5] == " ":
            return reply[6:]

        return reply

    def _query_stripped(self, cmd):
        return self._strip_reply_prefix(self._query(cmd), cmd=cmd)

    def _query_int(self, cmd, default=None):
        values = self._query_int_values(cmd)
        if values:
            return values[0]
        return default

    def _query_float(self, cmd, default=None):
        values = self._query_values(cmd)
        if values:
            try:
                return float(values[0])
            except Exception:
                return default
        return default

    def _query_values(self, cmd, retries=3):
        for attempt in range(retries):
            reply = self._query(cmd)
            values = self._strip_expected_reply_prefix(reply, cmd)
            if values is not None:
                return values.split()

            if self.__logger is not None:
                self.__logger.debug(
                    f"Ignoring unexpected Leica DMI reply for {cmd}: {reply}"
                )

        return None

    def _query_int_values(self, cmd, retries=3):
        values = self._query_values(cmd, retries=retries)
        if not values:
            return None

        try:
            return [int(value) for value in values]
        except Exception:
            return None

    def _strip_expected_reply_prefix(self, reply, cmd):
        if reply is None:
            return None

        reply = str(reply).strip()
        cmd = str(cmd).strip()

        if reply.startswith(cmd + " "):
            return reply[len(cmd) + 1:]
        if reply.startswith("$" + cmd + " "):
            return reply[len(cmd) + 2:]

        return None

    def _parse_position_and_label(self, cmd):
        reply = self._query_stripped(cmd)
        if reply is None:
            return None, None

        parts = str(reply).split(maxsplit=1)
        if len(parts) == 0:
            return None, None
        if len(parts) == 1:
            try:
                return int(parts[0]), None
            except Exception:
                return None, parts[0]

        try:
            return int(parts[0]), parts[1]
        except Exception:
            return None, str(reply)

    # ------------------------------------------------------------------
    # Legacy Z control
    # ------------------------------------------------------------------

    def move(self, value, *args):
        return self.move_z_relative_device_units(value)

    def setPosition(self, value, *args):
        return self.set_z_position_device_units(value)

    def move_z_relative_device_units(self, value):
        value = int(value)
        actual_position = self._position

        target_position = self._clamp_z_position_device_units(actual_position + value)
        delta = target_position - actual_position

        if delta != 0:
            cmd = "71024 " + str(delta)
            if abs(delta) > 132 and self.__logger is not None:
                self.__logger.warning("Leica DMI Z step bigger than 500 nm.")
            self._query(cmd)

        self._position = target_position
        return self._position

    def set_z_position_device_units(self, value):
        value = self._clamp_z_position_device_units(int(value))
        cmd = "71022 " + str(value)
        self._query(cmd)

        self._position = value
        return self._position

    def get_z_position_device_units(self):
        position = self._query_int("71023", default=self._position)
        if position is not None:
            self._position = position
        return position

    def get_z_upper_limit_device_units(self, refresh=False):
        if self._z_upper_limit_du is not None and not refresh:
            return self._z_upper_limit_du

        upper_limit = self._query_int("71056")
        if upper_limit is None:
            if self.__logger is not None:
                self.__logger.warning("Could not read Leica DMI Z upper limit with 71056.")
            return self._z_upper_limit_du

        self._z_upper_limit_du = upper_limit
        return self._z_upper_limit_du

    def has_z_position_um(self):
        return bool(self._lut_du_to_nm is not None or self._z_um_per_count is not None)

    def get_z_conversion_factor_um_per_count(self):
        return self._z_um_per_count

    def get_z_position_um(self):
        position_du = self.get_z_position_device_units()
        if position_du is None:
            return None

        return self._device_units_to_um(position_du)

    def set_z_position_um(self, value_um):
        position_du = self._um_to_device_units(value_um)
        position_du = self.set_z_position_device_units(position_du)
        return self._device_units_to_um(position_du)

    def move_z_relative_um(self, dist_um):
        actual_position_du = self._position

        delta_du = self._um_distance_to_device_units(dist_um)
        target_position_du = self._clamp_z_position_device_units(
            actual_position_du + delta_du
        )
        delta_du = target_position_du - actual_position_du

        if delta_du != 0:
            cmd = "71024 " + str(delta_du)
            self._query(cmd)

        self._position = target_position_du
        return self._device_units_to_um(self._position)

    def _clamp_z_position_device_units(self, value):
        value = int(value)
        clamped = max(self._z_lower_limit_du, value)

        upper_limit = self.get_z_upper_limit_device_units()
        if upper_limit is not None:
            clamped = min(upper_limit, clamped)

        if clamped != value and self.__logger is not None:
            self.__logger.warning(
                "Leica DMI Z target outside limits; clamped "
                f"{value} -> {clamped} device units."
            )

        return clamped

    def _device_units_to_um(self, value):
        value = float(value)

        if self._lut_du_to_nm is not None:
            value_um = float(self._lut_du_to_nm(value)) / 1000.0
        elif self._z_um_per_count is not None:
            value_um = value * self._z_um_per_count
        else:
            raise RuntimeError(
                "Leica DMI Z micrometer conversion is unavailable. Configure "
                "calibCsvPath or ensure command 71042 returns a conversion factor."
            )

        if not np.isfinite(value_um):
            raise RuntimeError(f"Leica DMI Z conversion returned non-finite value: {value_um}")

        return value_um

    def _um_to_device_units(self, value_um):
        value_um = float(value_um)

        if self._lut_nm_to_du is not None:
            value_du = float(self._lut_nm_to_du(value_um * 1000.0))
        elif self._z_um_per_count is not None:
            value_du = value_um / self._z_um_per_count
        else:
            raise RuntimeError(
                "Leica DMI Z micrometer conversion is unavailable. Configure "
                "calibCsvPath or ensure command 71042 returns a conversion factor."
            )

        if not np.isfinite(value_du):
            raise RuntimeError(f"Leica DMI Z conversion returned non-finite value: {value_du}")

        return int(round(value_du))

    def _um_distance_to_device_units(self, dist_um):
        dist_um = float(dist_um)

        if self._lut_nm_to_du is not None:
            zero_du = float(self._lut_nm_to_du(0.0))
            target_du = float(self._lut_nm_to_du(dist_um * 1000.0))
            value_du = target_du - zero_du
        elif self._z_um_per_count is not None:
            value_du = dist_um / self._z_um_per_count
        else:
            raise RuntimeError(
                "Leica DMI Z micrometer conversion is unavailable. Configure "
                "calibCsvPath or ensure command 71042 returns a conversion factor."
            )

        if not np.isfinite(value_du):
            raise RuntimeError(f"Leica DMI Z distance conversion returned non-finite value: {value_du}")

        return int(round(value_du))

    def get_pos_nm(self):
        try:
            pos_um = self.get_z_position_um()
        except RuntimeError as exc:
            if self.__logger is not None:
                self.__logger.warning(str(exc))
            return None

        if pos_um is None:
            return None

        pos_nm = pos_um * 1000.0
        if self.__logger is not None:
            self.__logger.debug(f"objective pos: {pos_nm}")
        return pos_nm

    def set_pos_nm(self, pos_nm):
        try:
            return self.set_z_position_um(float(pos_nm) / 1000.0)
        except RuntimeError as exc:
            if self.__logger is not None:
                self.__logger.warning(str(exc))
            return None

    def returnMod(self, reply):
        return self._strip_reply_prefix(reply)

    def position(self, *args):
        return str(self.get_z_position_device_units())

    def motCorrPos(self, value):
        movetopos = int(round(value * 93.83))
        cmd = "47022 -1 " + str(movetopos)
        self._write(cmd)

    # ------------------------------------------------------------------
    # Illumination mode commands
    # ------------------------------------------------------------------

    def setFLUO(self, *args):
        cmd = "70029 10 x"
        return self._query(cmd)

    def setCS(self, *args):
        cmd = "70029 14 x"
        return self._query(cmd)

    # ------------------------------------------------------------------
    # Shutters
    # ------------------------------------------------------------------

    def setILshutter(self, value):
        cmd = "77032 1 " + str(int(bool(value)))
        return self._query(cmd)

    def setTLshutter(self, value):
        cmd = "77032 0 " + str(int(bool(value)))
        return self._query(cmd)

    def getIlluminationMode(self):
        reply = self._query_stripped("77023")
        if reply is None:
            return None

        parts = str(reply).split()
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------
    # Cube turret
    # ------------------------------------------------------------------

    def getCube(self):
        return self._parse_position_and_label("78023")

    def setCube(self, slot):
        cmd = "78022 " + str(int(slot))
        self._write(cmd)
        return slot

    # ------------------------------------------------------------------
    # Side port
    # ------------------------------------------------------------------

    def getSidePort(self):
        return self._query_int("59023")

    def setSidePort(self, value):
        cmd = "59022 " + str(int(value))
        self._write(cmd)
        return value

    def setEyepiecePort(self):
        return self.setSidePort(1)

    def setCameraPort(self):
        return self.setSidePort(2)

    # ------------------------------------------------------------------
    # Magnification changer
    # ------------------------------------------------------------------

    def getMagnChanger(self):
        return self._query_int("79023")

    def setMagnChanger(self, value):
        cmd = "79022 " + str(int(value))
        self._write(cmd)
        return value

    def setMagn1(self):
        return self.setMagnChanger(1)

    def setMagnScan(self):
        return self.setMagnChanger(4)

    # ------------------------------------------------------------------
    # IL diaphragms
    # ------------------------------------------------------------------

    def getILFieldDiaphragm(self):
        return self._query_int("94023")

    def setILFieldDiaphragm(self, value):
        cmd = "94022 " + str(int(value))
        self._write(cmd)
        return value

    def getILApertureDiaphragm(self):
        return self._query_int("95023")

    def setILApertureDiaphragm(self, value):
        cmd = "95022 " + str(int(value))
        self._write(cmd)
        return value

    # ------------------------------------------------------------------
    # Calibration LUT
    # ------------------------------------------------------------------

    def create_lut_from_calib(self, calib_csv_path):
        data = np.loadtxt(calib_csv_path)
        data[:, 1] *= 1E6
        self._lut_nm_to_du = interp1d(data[:, 1], data[:, 0], bounds_error=False)
        self._lut_du_to_nm = interp1d(data[:, 0], data[:, 1], bounds_error=False)
