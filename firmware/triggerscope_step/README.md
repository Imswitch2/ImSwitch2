# TriggerScope step firmware

The TriggerScope 16 R4 as an `IMSWITCH_STEP/1` step board. The documentation
is `docs/devices/triggerscope-firmware.rst` (building, flashing, testing,
the setup entry); the protocol is `docs/devices/step-engine-protocol.rst`.

- `TriggerScopeStep/` — the Arduino sketch: `step_core.{h,cpp}` (the protocol
  and executor, platform independent), `TriggerScopeStep.ino` (the board),
  `Adafruit_MCP23017.{h,cpp}` (the expander driver the old sketch used).
- `host/` — the same core as a fake board on a PC (`make`, then it speaks the
  protocol on stdin/stdout); `imswitch/imcontrol/_test/unit/test_step_board_firmware_host.py`
  runs the conformance programs against it.
