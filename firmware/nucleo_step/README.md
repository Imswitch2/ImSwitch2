# Nucleo step firmware

The polarization rig's timing board, a NUCLEO-H7A3ZI-Q, as an `IMSWITCH_STEP/1`
step board (`docs/devices/step-engine-protocol.rst`): ImSwitch's
`StepProtocolScanEngine` uploads each scan program and the board runs it. It
replaces the rig's old `trigger_generator` firmware, which played buffers the
Microscope_GUI computed; the pins stay the same.

| Signal | Pin | Protocol channel | Setup (`nucleo_polarization_mock_setup.json`) |
| --- | --- | --- | --- |
| Galvo X | PA4 (DAC1 OUT1) | `dac0`, 0-3.3 V, 12 bits | `GalvoX`, `nucleo/dac0` |
| Galvo Y | PA5 (DAC1 OUT2) | `dac1`, 0-3.3 V, 12 bits | `GalvoY`, `nucleo/dac1` |
| 405 modulation | PA0 | `ttl0` | `405`, `nucleo/ttl0` |
| 488 modulation | PA1 | `ttl1` | `488`, `nucleo/ttl1` |
| Camera trigger | PA2 | `ttl2` | `Hamamatsu`, `nucleo/ttl2` |
| Board falling behind | LD3 (red) | -- | -- |

The board declares a 1 µs grid, both DACs, three lines, no trigger input, and a
heartbeat every 0.5 s. At power-up the galvos stand at 1.2 V and 1.6 V, as with
the old firmware; the lines are low.

## What is where

- `NucleoStep/nucleo_step.cpp`: the board layer -- the clock, the USB link's
  queues, the outputs, what the board declares. The protocol and executor are
  the step-engine core, `../triggerscope_step/TriggerScopeStep/step_core.{h,cpp}`
  (the TriggerScope runs the same core), compiled *inside* this file with the
  Nucleo's larger capacities. Do not compile `step_core.cpp` on its own.
- `NucleoStep/nucleo_hw.h`: the few things the board layer needs from the
  hardware; `NucleoStep/nucleo_hw_stm32.c` provides them on the board.
- `host/`: the same firmware on a PC, over a fake of `nucleo_hw.h`.
  `imswitch/imcontrol/_test/unit/test_nucleo_firmware_host.py` runs the
  protocol's conformance programs and the rig's four modes on it, and checks
  that a window over many steps is one pulse on its pin.

## Build it in STM32CubeIDE

Start from the old project (`trigger_generator`, STM32CubeIDE 1.19), so the
board, USB and DAC configuration stay as they are. Keep the old project: its
flash backup (`STM32_firmware_backup.bin`) puts the old firmware back with
STM32CubeProgrammer.

1. In the Project Explorer, copy and paste `trigger_generator` as `nucleo_step`.
2. Open `nucleo_step.ioc`, tab **Clock Configuration**: set **HCLK** to 280 MHz
   and let CubeMX solve the rest (PLL1, the bus dividers, the flash wait
   states). The board keeps time with the CPU clock, so a better clock is
   better time: with **HSE** as *BYPASS Clock Source* (the ST-LINK's 8 MHz)
   the PLL runs from a crystal instead of the internal RC oscillator.
   TIM6 and its DMA are no longer used; removing them is optional.
   **Project > Generate Code**.
3. Right-click the project: **Convert to C++**.
4. **Project > Properties > C/C++ General > Paths and Symbols**:
   - *Includes*, for GNU C and GNU C++: add
     `<repo>/firmware/nucleo_step/NucleoStep` and
     `<repo>/firmware/triggerscope_step/TriggerScopeStep`.
   - *Source Location* > *Link Folder...*: `<repo>/firmware/nucleo_step/NucleoStep`
     -- this folder only. The TriggerScope folder is on the include path, not a
     source folder.
5. **Project > Properties > C/C++ Build > Settings > MCU/MPU Settings**:
   *Runtime library*: **Standard C** (not *Reduced C*, `--specs=nano.specs`):
   the core prints 64-bit integers and doubles, which newlib-nano does not.
6. Replace the USER CODE sections below in `Core/Src/main.c` and
   `USB_DEVICE/App/usbd_cdc_if.c`. You can delete `Core/Src/cdc_utils.c`,
   `Core/Src/cache_utils.c` and their headers, which only the old firmware used.
7. Build (*Release*) and flash (*Run*, or STM32CubeProgrammer).

### `Core/Src/main.c`

| USER CODE section | Content |
| --- | --- |
| `Includes` | `#include "nucleo_step.h"` |
| `PV` | empty: the old buffers took 512 kB the core now needs |
| `0` | empty: the old playback functions |
| `2` | `nucleo_step_main();   /* never returns */` |

### `USB_DEVICE/App/usbd_cdc_if.c`

`INCLUDE`:

```c
#include "nucleo_step.h"
```

`PV` and `PRIVATE_FUNCTIONS_DECLARATION`: empty (the old sequence parser).

`11`, in `CDC_Receive_HS`:

```c
  if (nucleo_step_usb_received(Buf, *Len)) {
    USBD_CDC_SetRxBuffer(&hUsbDeviceHS, &UserRxBufferHS[0]);
    USBD_CDC_ReceivePacket(&hUsbDeviceHS);
  }
  return (USBD_OK);
```

`14`, in `CDC_TransmitCplt_HS`, after the `UNUSED(...)` lines:

```c
  nucleo_step_usb_sent();
```

`PRIVATE_FUNCTIONS_IMPLEMENTATION`:

```c
void cdc_rearm(void)
{
  USBD_CDC_SetRxBuffer(&hUsbDeviceHS, &UserRxBufferHS[0]);
  USBD_CDC_ReceivePacket(&hUsbDeviceHS);
}
```

## First run on the board

1. With the galvo drivers and the lasers off (the conformance programs move
   `dac0` and switch `ttl0`-`ttl2`), from the repository:

   ```bash
   python tools/step_board_conformance.py --port COM12 --baudrate 115200 --reply-timeout 0.5
   ```

   Six programs, each `PASS`.
2. In the setup, replace the Nucleo's `"port": "simulated"` and
   `simulatedBoard` with `"port": "COM12", "baudrate": 115200, "replyTimeoutS": 0.5`.
   **Hardware > What this setup can scan...** should list the board as above.
3. On an oscilloscope, PA0-PA2 and PA4-PA5, run the rig's programs
   (`imswitch/imcontrol/model/scan_program/legacy_microscope_gui.py`) and
   compare with the old firmware's output.
4. Watch LD3. It lights when an output of the running program goes out more
   than 10 µs late, which means the board is falling behind its steps: the
   Galvo Scan's steps are 8 µs (`step_samples=2`); `step_samples=4` doubles
   them. The LED clears at the next run.

## Test it on a PC

On Linux or macOS, `make` in `host/`; the test builds it itself. On Windows,
with Visual Studio's C++ tools:

```bash
firmware/nucleo_step/host/build_msvc.cmd C:/temp/nucleo_host.exe
```

then set `IMSWITCH_NUCLEO_HOST=C:/temp/nucleo_host.exe` and run
`pytest imswitch/imcontrol/_test/unit/test_nucleo_firmware_host.py`.

Copyright (C) 2026 ImSwitch developers. GPL-3.0-or-later.
