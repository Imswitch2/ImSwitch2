# imswitch-zhinst-devices

Example Zurich Instruments device plugin for ImSwitch2.

This package is the first concrete example for the proposed ImSwitch2 device
plugin architecture. It targets Zurich Instruments lock-in devices through
`zhinst-toolkit` and exposes demodulator samples as an ImSwitch detector.

The first contributed manager is:

```text
zhinst.lockin-demod
```

It is intended for MFLI, UHFLI, HF2LI, and similar devices that expose lock-in
demodulator sample nodes. It uses Zurich Instruments' subscribe/poll workflow:
subscribe to `device.demods[demodIndex].sample`, poll the session, and extract
one signal such as `x`, `y`, `r`, `phase`, or `frequency`.

Relevant Zurich Instruments references:

- `zhinst-toolkit`: https://github.com/zhinst/zhinst-toolkit
- Toolkit examples: https://docs.zhinst.com/zhinst-toolkit/en/latest/examples/index.html
- Demodulator polling example: https://docs.zhinst.com/zhinst-toolkit/en/latest/examples/hf2.html
- `Session.connect_device`: https://docs.zhinst.com/zhinst-toolkit/en/latest/_autosummary/zhinst.toolkit.session.Session.connect_device.html

## Why this is a good first plugin

- Zurich Instruments already maintains a Python 3.10+ high-level package.
- Hardware dependencies can stay outside ImSwitch core.
- The first manager can be useful without touching timing-critical scan code.
- A mock mode can generate deterministic lock-in-like frames without LabOne.
- The manager exercises plugin manifests, setup templates, schemas, optional
  hardware extras, and fallback imports.

## Install

Development install from this directory:

```bash
python -m pip install -e ".[test]"
```

For real hardware support:

```bash
python -m pip install -e ".[hardware,test]"
```

`zhinst-toolkit` requires the Zurich Instruments LabOne stack and a running
LabOne data server. The mock mode does not require LabOne.

## Setup Template

The included mock setup template is:

```text
src/imswitch_zhinst_devices/setup_templates/zhinst_lockin_mock.json
```

The relevant detector block is:

```json
{
  "managerName": "zhinst.lockin-demod",
  "managerProperties": {
    "deviceSerial": "DEVXXXX",
    "serverHost": "localhost",
    "demodIndex": 0,
    "signal": "r",
    "sampleCount": 256,
    "frameShape": [16, 16],
    "pollDurationS": 0.05,
    "useMock": true,
    "useMockOnFailure": true
  }
}
```

For hardware, set `useMock` to `false` and replace `deviceSerial` with the
LabOne serial, for example `DEV1234`.

Optional hardware connection properties:

- `serverHost`: LabOne data server host, default `localhost`.
- `serverPort`: LabOne data server port. Omit for toolkit defaults.
- `hf2`: set true for HF2 devices that use the HF2 data server.
- `interface`: optional device interface passed to `Session.connect_device`,
  for example `1GbE` or `USB`.
- `allowVersionMismatch`: forwarded to `Session`.

## Current Limits

This is a first plugin example, not a production lock-in integration.

Known limits:

- Samples are exposed as a small 2D frame for compatibility with the current
  `DetectorManager`/viewer path.
- It does not yet synchronize lock-in samples with ImSwitch scan waveforms.
- It does not configure demodulator settings beyond optional enable/rate nodes.
- It has no custom widget; parameters are exposed through detector parameters.

## License

GPL-3.0-or-later, matching the intended ImSwitch in-process plugin policy.
