from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.graph import (
    DeviceDependencySpec, DeviceDescriptorSpec, DeviceRelationKind,
    DeviceRole, HardwareDeviceId,
)
from imswitch.imcontrol.model.devices.status import DeviceId


class MockLeicaDMIStandManager:
    def __init__(self, deviceInfo, *args, **kwargs):
        self.__logger = initLogger(self)
        self._deviceInfo = deviceInfo
        try:
            self._rs232Manager = kwargs['rs232sManager']._subManagers[deviceInfo.rs232device]
        except Exception as e:
            self.__logger.error(f'Failed to access Leica DMI stand RS232 connection with name {deviceInfo.rs232device}: {e}. Define it in your setup .json. Loading mocker.')
            from imswitch.imcontrol.model.interfaces.RS232Driver_mock import MockRS232Driver
            self._rs232Manager = MockRS232Driver(name=deviceInfo.rs232device, settings={'port': 'Mock'})

    def getDeviceDescriptorSpec(self):
        rs232_name = self._deviceInfo.rs232device
        return DeviceDescriptorSpec(
            role=DeviceRole.PRIMARY,
            hardware_id=HardwareDeviceId('stand', f'leica:{rs232_name}'),
            display_name='Leica stand',
            category='stand',
            dependencies=(
                DeviceDependencySpec(
                    DeviceRelationKind.USES_TRANSPORT,
                    target=DeviceId('rs232', str(rs232_name)),
                    label=str(rs232_name),
                ),
            ),
        )

    def move(self, value, *args):
        if not int(value) == 0:
            cmd = str(int(value))
            self._rs232Manager.write(cmd)

        self._position = self._position + value
        return self._position

    def setPosition(self, value, *args):
        cmd = str(int(value))
        self._rs232Manager.write(cmd)

        self._position = value
        return self._position

    def returnMod(self, reply):
        return reply

    def position(self, *args):
        cmd = '000'
        return self.returnMod(self._rs232Manager.send(cmd))

    def motCorrPos(self, value):
        """ Absolute mot_corr position movement. """
        movetopos = int(round(value))
        cmd = str(movetopos)
        self._rs232Manager.write(cmd)

    # the serial command automatically sleeps until a reply is gotten, which it gets after flip is finished
    def setFLUO(self, *args):
        cmd = '000'
        self._rs232Manager.query(cmd)

    # the serial command automatically sleeps until a reply is gotten, which it gets after flip is finished
    def setCS(self, *args):
        cmd = '000'
        self._rs232Manager.query(cmd)

    def setILshutter(self, value):
        cmd = str(value)
        self._rs232Manager.query(cmd)
        
    def setTLshutter(self, value):
        cmd = str(value)
        self._rs232Manager.query(cmd)


# Backward-compatible setup/import names used by existing Leica stand setups.
MockLeicaDMIManager = MockLeicaDMIStandManager
LeicaDMIManager_mock = MockLeicaDMIStandManager
