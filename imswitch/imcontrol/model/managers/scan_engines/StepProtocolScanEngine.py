"""A board that speaks the step-engine protocol, IMSWITCH_STEP/1, as a scan
engine (docs/design/plans/scan-engines-and-expert-scan.md §5.2, M5;
``docs/devices/step-engine-protocol.rst`` is the protocol).

Any board that implements the protocol -- the TriggerScope once the lab
rewrites its firmware, a Teensy, an Arduino, an ESP32 -- joins without
Python: this engine asks it ``*IDN?`` and ``*CAPS?``, and what the board
answers is what the engine can do. A scan program is compiled on the host
into the board's terms and uploaded; the board runs it on its own and reports
progress, releases and the end; **Stop** stops it at its next step, at once
while it waits; ``LOG?`` returns what it executed, for conformance with the
interpreter.

What the host owns, the board is sent:

- **positions** in volts, from each positioner's ``conversionFactor`` (µm per
  volt) -- a positioner that declares none is refused as unknown -- and
  checked against the board's DAC range and the positioner's
  ``minVolt``/``maxVolt``;
- **polarity**: every line's electrical on and off level from the device's
  ``ttlActiveLevel``, and the off level as its **safe** level, where a stop,
  a failure and the end of a run leave it. DACs hold their positions.

Channels are named ``<engine>/dac<i>`` and ``<engine>/ttl<j>``; a device is
wired to one by its ``analogChannel`` or ``digitalLine``.

``managerProperties``:

- ``port`` (required): the board's serial port, or ``"simulated"`` for the
  simulated board (``step_board.SimulatedStepBoard``);
- ``baudrate``, ``replyTimeoutS`` (required for a serial port): the link's
  rate, and how long a command waits for its answer;
- ``simulatedBoard`` (required for ``"simulated"``): what the simulated board
  declares -- ``gridNs`` (required), ``dacs`` (``{index: [minV, maxV]}``),
  ``ttlLines``, ``maxSteps``, ``triggerIn``, ``triggerLatencyTicks``, and
  ``actualTriggerLatencyTicks`` when it has a trigger input whose latency it
  does not declare.
"""

from __future__ import annotations

import dataclasses
import math
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.scan_program.engine import (
    AnalogOut, EngineCapabilities, FeatureVerdict, IMPOSSIBLE, UNKNOWN, ScanEngineRefused,
)
from imswitch.imcontrol.model.signaldesigners.basesignaldesigners import (
    scan_voltage_range_refusal,
)
from .SimulatedStepEngine import Compiled, StepEngineBase
from .step_board import SimulatedStepBoard
from .step_executor import RunLog
from .step_protocol import (
    BoardCaps, INT32_MAX, INT32_MIN, ProtocolError, UINT32_MAX, WirePlan, channel, message,
    parse_caps, parse_idn, parse_log, plan_to_lines, text,
)

NS_PER_S = 1_000_000_000
SIMULATED = 'simulated'
#: A line's electrical levels on the wire.
TTL_LOW, TTL_HIGH = 0, 1


class StepBoardError(RuntimeError):
    """The board refused, did not answer, or the link failed."""


class SimulatedTransport:
    """The line link to an in-process board, which answers a command before
    ``write`` returns: a command's answer is read without waiting."""
    reply_timeout = 0
    run_timeout = None

    def __init__(self, board: SimulatedStepBoard):
        self.board = board

    def write(self, line):
        self.board.write(line)

    def read(self, timeout):
        return self.board.read(timeout)

    def close(self):
        pass


class SerialTransport:
    """The line link to a board on a serial port (pyserial)."""

    def __init__(self, port, baudrate, reply_timeout):
        import serial
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=reply_timeout)
        self.reply_timeout = reply_timeout
        # A run sends nothing while it waits for a trigger: the follower
        # reads in reply-timeout slices so a close is noticed.
        self.run_timeout = reply_timeout
        self.board = None
        self._partial = b''

    def write(self, line):
        self._serial.write((line + '\n').encode('ascii'))

    def read(self, timeout):
        self._serial.timeout = timeout
        raw = self._serial.readline()
        if not raw.endswith(b'\n'):
            self._partial += raw
            return None
        raw, self._partial = self._partial + raw, b''
        return raw.decode('ascii', errors='replace').strip()

    def close(self):
        self._serial.close()


def simulated_board(props) -> SimulatedStepBoard:
    """The simulated board a ``simulatedBoard`` property describes."""
    if 'gridNs' not in props:
        raise ValueError('simulatedBoard.gridNs is required (the board\'s timing grid, '
                         'in nanoseconds).')
    caps = BoardCaps(
        board=str(props.get('board', SIMULATED)), firmware=str(props.get('firmware', SIMULATED)),
        grid_ns=int(props['gridNs']),
        dacs={int(i): (float(r[0]), float(r[1])) for i, r in (props.get('dacs') or {}).items()},
        ttl_lines=int(props.get('ttlLines', 0)), max_steps=props.get('maxSteps'),
        trigger_in=bool(props.get('triggerIn', False)),
        trigger_latency_ticks=props.get('triggerLatencyTicks'))
    return SimulatedStepBoard(caps, latency_ticks=props.get('actualTriggerLatencyTicks'))


@dataclass
class ProtocolCompiled(Compiled):
    wire: Optional[WirePlan] = None
    lines: List[str] = field(default_factory=list)
    #: ``{channel: (device, µm per volt or None)}``: the board's log back in
    #: the program's terms.
    back: Dict[str, Tuple[str, Optional[float]]] = field(default_factory=dict)


class StepProtocolScanEngine(StepEngineBase):
    """A board that speaks IMSWITCH_STEP/1."""

    _capsSource = '*CAPS?'

    def __init__(self, engineInfo, name='StepProtocolScanEngine', setupInfo=None,
                 transport=None, **_lowLevelManagers):
        super().__init__(name)
        self.__logger = initLogger(self, instanceName=name)
        props = dict(getattr(engineInfo, 'managerProperties', None) or {})
        self._setupInfo = setupInfo
        self._open = None
        self._transport = transport
        if transport is None:
            port = props.get('port')
            if not port:
                raise ValueError(f'{name}: managerProperties.port is required (a serial port, '
                                 f'or "{SIMULATED}").')
            if port == SIMULATED:
                if 'simulatedBoard' not in props:
                    raise ValueError(f'{name}: managerProperties.simulatedBoard is required on '
                                     'the simulated port (what the board declares).')
                self._transport = SimulatedTransport(simulated_board(props['simulatedBoard']))
            else:
                baudrate, replyTimeout = props.get('baudrate'), props.get('replyTimeoutS')
                missing = [key for key, value in (('baudrate', baudrate),
                                                  ('replyTimeoutS', replyTimeout))
                           if value is None]
                if missing:
                    raise ValueError(f'{name}: managerProperties.{" and ".join(missing)} '
                                     f'{"is" if len(missing) == 1 else "are"} required on a '
                                     'serial port.')
                self._open = lambda: SerialTransport(port, int(baudrate), float(replyTimeout))
        self._io = threading.Lock()
        self._caps: Optional[BoardCaps] = None
        self._unreachable = 'not asked yet'
        self._prepared: Optional[ProtocolCompiled] = None
        self._running = False
        self._abortPending = False
        self._closing = False
        self._follower = None
        self._log: Optional[RunLog] = None
        self.progress = None
        self.connect()

    @property
    def board(self) -> Optional[SimulatedStepBoard]:
        """The simulated board, on the simulated port."""
        return getattr(self._transport, 'board', None)

    # -- the board ----------------------------------------------------------------

    def connect(self) -> bool:
        """Ask the board who it is and what it can do; False (and the reason,
        in the report) when it does not answer as a step board."""
        try:
            if self._transport is None:
                self._transport = self._open()
            board, firmware = parse_idn(self._ask('*IDN?'))
            self._write('*CAPS?')
            caps = parse_caps(board, firmware, self._readUntil('CAPS,END'))
        except (OSError, ProtocolError, StepBoardError) as error:
            self._unreachable = text(error)
            self._setCaps(None)
            self.__logger.warning(f'{self.name}: the board does not answer as a step board: '
                                  f'{self._unreachable}')
            if self._open is not None and self._transport is not None:
                self._transport.close()
                self._transport = None
            return False
        self._setCaps(caps)
        self.__logger.info(f'{self.name}: {caps.board} {caps.firmware} ({len(caps.dacs)} DACs, '
                           f'{caps.ttl_lines} lines)')
        return True

    def _setCaps(self, caps):
        self._caps = caps
        grid = None if caps is None or caps.grid_ns is None else caps.grid_ns / NS_PER_S
        self._grid = grid
        self._analog = {} if caps is None else {
            f'{self.name}/dac{i}': AnalogOut(f'{self.name}/dac{i}', low, high)
            for i, (low, high) in caps.dacs.items()}
        self._digital = () if caps is None else tuple(
            f'{self.name}/ttl{j}' for j in range(caps.ttl_lines))
        latency = None if caps is None else caps.trigger_latency_ticks
        self._latency = None if latency is None or grid is None else latency * grid
        self._maxSteps = None if caps is None else caps.max_steps
        self._waits = frozenset({'time'} | ({'start-trigger', 'step-trigger'}
                                            if caps is not None and caps.trigger_in else set()))

    def capabilities(self) -> EngineCapabilities:
        caps = super().capabilities()
        source = f'{self.name} *CAPS?'
        return dataclasses.replace(
            caps, safe_state='every line at the safe level the host sent, DACs holding',
            sources={'timing_grid_s': f'{source} grid_ns', 'max_steps': f'{source} max_steps',
                     'trigger_latency_s': f'{source} trigger_latency_ticks'})

    def owns(self, channel_name: str) -> bool:
        prefix, _, token = str(channel_name).partition('/')
        if prefix != self.name:
            return False
        try:
            channel(token)
        except ProtocolError:
            return False
        # Until the board answers, its channels are its by name: they never
        # pass to another engine because a board is unplugged.
        return self._caps is None or self._caps.has(token)

    # -- what it can run ------------------------------------------------------------

    def assess(self, program, channels):
        report = super().assess(program, channels)
        if self._caps is None:
            report.add('board', UNKNOWN, f'{self.name} has not answered: {self._unreachable}',
                       'Connect the board; the next compile asks it again.')
            return report
        if self._caps.grid_ns is None:
            report.add('timing grid', UNKNOWN,
                       f'{self.name} does not declare its timing grid (CAPS grid_ns).')
        for event in program.timeline:
            if event.kind == 'level':
                _replace(report, f'level {event.device}', IMPOSSIBLE,
                         f'A power level of {event.device} needs its power-to-volt conversion; '
                         f'{self.name} plays lines and positioner drives only.')
        for wait in program.waits:
            if wait.hold != 'idle':
                report.add(f'wait hold at {wait.at}', IMPOSSIBLE,
                           f'A board waits idle (positions held, lines off); not '
                           f'{wait.hold!r}.')
        return report

    def compile(self, program, channels, captured_um=None, devices=None) -> ProtocolCompiled:
        if self._caps is None:
            self.connect()
        compiled = super().compile(program, channels, captured_um, devices)
        wire, back, refusals, unknown = self._wire(compiled.plan, channels)
        if unknown:
            compiled.report.add('conversion', UNKNOWN, ' '.join(unknown))
        if refusals:
            compiled.report.add('board', IMPOSSIBLE, ' '.join(refusals))
        if unknown or refusals:
            raise ScanEngineRefused(compiled.report)
        return ProtocolCompiled(compiled.program, compiled.plan, compiled.report, wire,
                                plan_to_lines(wire), back)

    def _wire(self, plan, channels):
        """The plan in the board's terms: channels, volts, electrical levels."""
        refusals, unknown, back = [], [], {}

        def token(line):
            return str(line).partition('/')[2]

        drives, factors = [], {}
        for d in plan.drives:
            factor = self._conversion(d['device'])
            if factor is None:
                unknown.append(f'{d["device"]} declares no conversionFactor (µm per volt).')
                continue
            name = token(channels[d['device']].analog)
            factors[d['device']] = factor
            back[name] = (d['device'], factor)
            drives.append(dict(
                d, device=name, channel=name, offset=d['offset'] / factor,
                weights={k: w / factor for k, w in d['weights'].items()},
                table=None if d['table'] is None else [v / factor for v in d['table']]))
        if unknown:
            return None, back, refusals, unknown

        def volts(positions):
            return {back_name: positions[device] / factors[device]
                    for back_name, (device, _) in back.items() if device in positions}

        captured, park = volts(plan.captured_um), volts(plan.park_um)
        for d, wire in zip(plan.drives, drives):
            low, high = _extremes(wire, plan, captured.get(wire['device'], 0.0))
            parked = park.get(wire['device'])
            if parked is not None:
                low, high = min(low, parked), max(high, parked)
            dac_low, dac_high = self._caps.dacs[channel(wire['device'])[1]]
            if low < dac_low or high > dac_high:
                refusals.append(f'{d["device"]} needs {low:g} to {high:g} V; {self.name} '
                                f'{wire["device"]} gives {dac_low:g} to {dac_high:g} V.')
            refusal = scan_voltage_range_refusal(self._setupInfo, d['device'], (low, high))
            if refusal:
                refusals.append(refusal)

        events, safe, carriers = [], {}, {}
        for e in plan.events:
            line = e['device'] if e['kind'] == 'marker' else channels[e['device']].digital
            name = token(line)
            if carriers.setdefault(name, e['device']) != e['device']:
                refusals.append(f'{name} carries {carriers[name]} and {e["device"]}; a line '
                                'switches one device.')
                continue
            active = self._activeLevel(e['device'])
            if active not in ('high', 'low'):
                refusals.append(f'{e["device"]}: ttlActiveLevel must be "high" or "low", not '
                                f'{active!r}.')
                continue
            on, off = (TTL_HIGH, TTL_LOW) if active == 'high' else (TTL_LOW, TTL_HIGH)
            back[name] = (e['device'], None)
            safe[name] = off
            events.append(dict(e, device=name, on=on, off=off))
        preparation = {}
        for device, ticks in plan.preparation.items():
            entry = channels.get(device)
            if entry is None or entry.digital is None:
                refusals.append(f'{device} needs preparation and has no line on {self.name}.')
                continue
            preparation[token(entry.digital)] = ticks
        wire = WirePlan(loops=plan.loops, phase_loop=plan.phase_loop, drives=drives,
                        step_ticks=plan.step_ticks, return_ticks=plan.return_ticks,
                        events=events, waits=plan.waits, preparation=preparation,
                        captured_um=captured, park_um=park, safe=safe)
        refusals += _width_refusals(wire)
        return wire, back, refusals, unknown

    def _conversion(self, device) -> Optional[float]:
        positioners = getattr(self._setupInfo, 'positioners', None) or {}
        props = getattr(positioners.get(device), 'managerProperties', None) or {}
        factor = props.get('conversionFactor')
        return None if factor is None else float(factor)

    def _activeLevel(self, device):
        """The device's ``ttlActiveLevel``; a device that declares none is
        active high (``SetupInfo``'s rule)."""
        for section in ('lasers', 'detectors'):
            info = (getattr(self._setupInfo, section, None) or {}).get(device)
            if info is not None:
                return getattr(info, 'ttlActiveLevel', None) or 'high'
        return 'high'

    # -- a run ----------------------------------------------------------------------

    def prepare(self, compiled: ProtocolCompiled):
        """Upload the program; StepBoardError when the board refuses it."""
        self._idle('upload a program')
        self._abortPending = False
        self._prepared = None
        self._write(*compiled.lines)
        self._expect('PROGRAM')
        self._prepared = compiled
        devices = sorted({e['device'] for e in compiled.plan.events}
                         | {d['device'] for d in compiled.plan.drives})
        self.sigScanBuilt.emit({'engine': self.name}, compiled.plan, devices)

    def ready(self) -> bool:
        return self._prepared is not None

    def run(self, compiled: ProtocolCompiled = None, *, arrivals=(), blocking=False,
            fail_at_step=None):
        """Start the uploaded program. ``arrivals`` and ``fail_at_step`` are
        the simulated board's trigger input and an injected failure."""
        self._idle('start a run')
        if compiled is not None and compiled is not self._prepared:
            self.prepare(compiled)
        if self._prepared is None:
            raise RuntimeError(f'{self.name}: nothing prepared to run.')
        board = self.board
        if board is None and (arrivals or fail_at_step is not None):
            raise ValueError('Trigger arrivals and an injected failure are a simulated '
                             'board\'s; a real board takes its trigger input.')
        if board is not None:
            board.arrivals, board.fail_at_step = tuple(arrivals), fail_at_step
        if self._abortPending:
            # Stopped before it started: the board is not started at all
            # (RUN then STOP would race it), and nothing ran.
            self._abortPending = False
            self._log = RunLog()
            self.sigScanStopped.emit()
            self.sigScanDone.emit()
            return
        self._log = None
        self._write('RUN')
        self._expect('RUN')
        self._running = True
        if blocking:
            self._follow()
        else:
            self._follower = threading.Thread(target=self._follow, daemon=True,
                                              name=f'{self.name} run')
            self._follower.start()

    def abort(self) -> str:
        if self._running:
            self._write('STOP')
        else:
            # Between prepare and run: the run does not start.
            self._abortPending = True
        return 'step-boundary'

    def join(self, timeout=None):
        if self._follower is not None:
            self._follower.join(timeout)

    def _follow(self):
        """Read what the board reports until its run ends; then signal."""
        outcome, reason = 'failed', ''
        transport = self._transport
        try:
            while True:
                line = transport.read(transport.run_timeout)
                if line is None:
                    if self._closing:
                        reason = 'the link was closed during the run'
                        break
                    continue
                kind, fields = message(line)
                if kind == 'STARTED':
                    self.sigScanStarted.emit()
                elif kind == 'STEP':
                    self.progress = (int(fields[0]), int(fields[1]))
                elif kind == 'RELEASE':
                    self.sigBarrierReleased.emit(int(fields[0]), int(fields[1]))
                elif kind in ('DONE', 'STOPPED'):
                    outcome = kind.lower()
                    break
                elif kind == 'FAILED':
                    reason = f'failed at step {fields[0]}: {fields[1]}'
                    break
                elif kind == 'ERR':
                    reason = f'{fields[0]}: {fields[1]}'
                    break
                # PREPARED, and OK,STOP for a stop sent while running.
        except (OSError, ProtocolError, ValueError, IndexError) as error:
            reason = f'the link failed: {text(error)}'
        finally:
            self._running = False
        if outcome == 'done':
            self.sigScanDone.emit()
        elif outcome == 'stopped':
            self.sigScanStopped.emit()
            self.sigScanDone.emit()
        else:
            self.__logger.error(f'{self.name}: {reason}')
            self.sigScanFailed.emit(reason)
            self.sigScanBuildFailed.emit()

    @property
    def log(self) -> RunLog:
        """What the board executed in its last run, in the program's terms
        (devices, µm), asked for with ``LOG?`` once the run is over."""
        if self._log is None:
            if self._running:
                raise RuntimeError(f'{self.name} is running; its log comes after.')
            self._write('LOG?')
            first = self._read('LOG?')
            kind, fields = message(first)
            if kind == 'ERR':
                raise StepBoardError(f'{self.name} keeps no log: {fields[0]}: {fields[1]}')
            log = parse_log([first] if first == 'LOG,END' else
                            [first] + self._readUntil('LOG,END'))
            self._log = _translated(log, self._prepared.back if self._prepared else {})
        return self._log

    # -- the link ---------------------------------------------------------------------

    def _idle(self, what):
        # While a run is followed, its thread alone reads the link.
        if self._running:
            raise StepBoardError(f'{self.name} is running; it cannot {what} until the run '
                                 'ends.')

    def _write(self, *lines):
        if self._transport is None:
            raise StepBoardError(f'{self.name} is not connected: {self._unreachable}')
        with self._io:
            for line in lines:
                self._transport.write(line)

    def _read(self, what):
        line = self._transport.read(self._transport.reply_timeout)
        if line is None:
            raise StepBoardError(f'{self.name} did not answer {what}.')
        return line

    def _ask(self, command):
        self._write(command)
        return self._read(command)

    def _readUntil(self, last):
        lines = []
        while True:
            line = self._read(last)
            lines.append(line)
            if line == last:
                return lines

    def _expect(self, command):
        """Read until the answer to ``command``: ``OK,<command>`` or ``ERR``.
        What a finished run left behind (a late OK,STOP) is passed over."""
        while True:
            kind, fields = message(self._read(command))
            if kind == 'OK' and fields and fields[0] == command:
                return fields[1:]
            if kind == 'ERR':
                raise StepBoardError(f'{self.name} refused {command}: {fields[0]}: '
                                     f'{fields[1]}')

    def finalize(self):
        """Stop a run, and close the link. A board that does not report the
        stop is given up on at the follower's next empty read."""
        self._closing = True
        if self._running:
            self._write('STOP')
            self.join()
        transport, self._transport = self._transport, None
        self._unreachable = 'closed'
        if transport is not None:
            transport.close()


def _replace(report, feature, verdict, reason):
    for i, item in enumerate(report.verdicts):
        if item.feature == feature:
            report.verdicts[i] = FeatureVerdict(feature, verdict, reason)
            return
    report.add(feature, verdict, reason)


def _extremes(drive, plan, captured):
    """The lowest and highest value a drive takes: an affine drive over
    loops that visit every count reaches its extremes at their ends."""
    counts = {loop_id: count for loop_id, count, _, _ in plan.loops}
    low = high = drive['offset'] + (captured if drive['origin'] == 'captured' else 0.0)
    for loop_id, weight in drive['weights'].items():
        span = weight * (counts[loop_id] - 1)
        low, high = low + min(0.0, span), high + max(0.0, span)
    if drive['table'] is not None:
        low, high = low + min(drive['table']), high + max(drive['table'])
    if drive['restore'] == 'captured':
        low, high = min(low, captured), max(high, captured)
    return low, high


def _width_refusals(wire: WirePlan) -> List[str]:
    """What does not fit the protocol's integers."""
    out = []
    steps = math.prod(count for _, count, _, _ in wire.loops)
    unsigned = [(f'loop {i} count', count) for i, (_, count, _, _) in enumerate(wire.loops)]
    unsigned += [('the run', steps), ('step', wire.step_ticks), ('return', wire.return_ticks)]
    unsigned += [(f'{d["device"]} move', d['move']) for d in wire.drives]
    unsigned += [(f'{d["device"]} settle', d['settle']) for d in wire.drives]
    unsigned += [(f'wait {k}', w[k]) for w in wire.waits for k in ('interval', 'timeout')
                 if w[k] is not None]
    for what, value in unsigned:
        if not 0 <= value <= UINT32_MAX:
            out.append(f'{what}: {value} does not fit the protocol\'s unsigned 32-bit number.')
    for e in wire.events:
        for key in ('start', 'end'):
            if not INT32_MIN <= e[key] <= INT32_MAX:
                out.append(f'{e["device"]} {key}: {e[key]} ticks does not fit the protocol\'s '
                           'signed 32-bit number.')
    return out


def _translated(log: RunLog, back) -> RunLog:
    """A board's log with its channels as the program's devices and its
    volts as µm."""
    out = RunLog()
    for kind, *fields in log.entries:
        if kind == 'dac':
            segment, tick, name, value = fields
            device, factor = back.get(name, (name, None))
            out.add('dac', segment, tick, device, value if factor is None else value * factor)
        elif kind in ('on', 'off'):
            segment, tick, name = fields
            out.add(kind, segment, tick, back.get(name, (name, None))[0])
        else:
            out.add(kind, *fields)
    return out


__all__ = ['ProtocolCompiled', 'SIMULATED', 'SerialTransport', 'SimulatedTransport',
           'StepBoardError', 'StepProtocolScanEngine', 'simulated_board']


# Copyright (C) 2020-2026 ImSwitch developers
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
