# ImSwitch2

ImSwitch2 is a Python software for flexible, modular microscope control. It supports a wide range of hardware (cameras, lasers, stages, DAQ cards) through a configuration-driven manager system — no code changes needed to switch hardware.

This is a clean-slate continuation of the [ImSwitch](https://github.com/ImSwitch/ImSwitch) project, with a focus on safety, maintainability, and AI-assisted development.

---

## Installation

**Requirements:** Python 3.10 or newer, PyQt5.

### Core install (UI + file I/O, no hardware drivers)

```
pip install -e .
```

### With hardware drivers

Install the optional `hardware` extra to add NI-DAQ, Lantz, and pyVISA support:

```
pip install -e ".[hardware]"
```

Install the optional `full` extra to also add napari, OpenCV, and vispy:

```
pip install -e ".[full]"
```

### Launch

```
python -m imswitch
```

On first launch ImSwitch creates a configuration folder and opens a setup picker dialog if no setup file is configured yet.

---

## Configuration

ImSwitch reads all hardware configuration from a folder called **ImSwitchConfig** in your home directory:

```
~/ImSwitchConfig/               (Linux / macOS)
Documents\ImSwitchConfig\       (Windows)
  ├── config/
  │   └── imcontrol_options.json    # active setup filename + recording folder
  └── imcontrol_setups/
      └── my_microscope.json        # hardware definition (detectors, lasers, …)
```

The `imcontrol_setups/` folder is populated with example files on first launch. You can have multiple setup files and switch between them at startup.

### Using the Config Editor

A GUI editor is included for building and editing setup files without writing JSON by hand:

```
python utility_scripts/imswitch_config_editor.py
```

The editor loads built-in templates for every supported manager (cameras, lasers, stages, etc.), lets you add and configure devices visually, and saves a valid JSON file directly into `imcontrol_setups/`.

---

## Setting Up a Microscope from Scratch

### Step 1 — Create a setup file

Open the config editor and add the devices you have. Or create a JSON file in `~/ImSwitchConfig/imcontrol_setups/` manually (see examples below).

### Step 2 — Point ImSwitch at it

Edit `~/ImSwitchConfig/config/imcontrol_options.json`:

```json
{
  "setupFileName": "my_microscope.json",
  "recording": {
    "outputFolder": "~/Data",
    "includeDateInOutputFolder": true
  }
}
```

### Step 3 — Launch and verify

```
python -m imswitch
```

ImSwitch will report any missing hardware in the log. Devices that fail to connect are skipped; the rest of the UI still loads.

---

## Hardware Examples

### USB / Generic Camera (`AVManager`)

Suitable for any OpenCV-compatible camera (USB webcams, Allied Vision, etc.).

```json
"detectors": {
  "Camera": {
    "analogChannel": null,
    "digitalLine": null,
    "managerName": "AVManager",
    "managerProperties": {
      "cameraListIndex": 0,
      "avcam": {
        "exposure": 100,
        "gain": 1
      }
    },
    "forAcquisition": true
  }
}
```

Set `cameraListIndex` to the index of your camera in the system list (0 = first camera). Set it to the string `"mock"` to run without hardware connected.

---

### Hamamatsu sCMOS Camera (`HamamatsuManager`)

Requires the Hamamatsu DCAM SDK installed on the system.

```json
"detectors": {
  "Hamamatsu": {
    "analogChannel": null,
    "digitalLine": "Dev1/port0/line3",
    "managerName": "HamamatsuManager",
    "managerProperties": {
      "cameraListIndex": 0,
      "hamamatsu": {
        "readout_speed": 3,
        "trigger_source": 2,
        "trigger_active": 1,
        "trigger_polarity": 2,
        "exposure_time": 0.01,
        "subarray_hpos": 0,
        "subarray_hsize": 2048,
        "subarray_vpos": 0,
        "subarray_vsize": 2048
      }
    },
    "forAcquisition": true
  }
}
```

`digitalLine` is the NI-DAQ output that triggers the camera; set to `null` if not using hardware triggering.

---

### Cobolt 06-01 Laser — direct serial (`Cobolt0601NewLaserManager`)

No extra dependencies beyond `pyserial`. Connect the laser via USB-to-serial and find its COM port (e.g. in Device Manager on Windows, or `ls /dev/ttyUSB*` on Linux).

```json
"lasers": {
  "561 nm": {
    "analogChannel": null,
    "digitalLine": null,
    "managerName": "Cobolt0601NewLaserManager",
    "managerProperties": {
      "digitalPorts": ["COM4"]
    },
    "wavelength": 561,
    "valueRangeMin": 0,
    "valueRangeMax": 200
  }
}
```

Replace `"COM4"` with the correct port (`"/dev/ttyUSB0"` on Linux). `valueRangeMax` is in mW — set it to the maximum power of your laser.

**Note:** Use `Cobolt0601LaserManager` instead if you need Lantz-based instrument control (requires `pip install -e ".[hardware]"`). The new driver above is preferred for most setups.

---

### Minimal complete setup file

A minimal two-device setup (one camera, one laser, no DAQ):

```json
{
  "detectors": {
    "Camera": {
      "analogChannel": null,
      "digitalLine": null,
      "managerName": "AVManager",
      "managerProperties": {
        "cameraListIndex": 0,
        "avcam": { "exposure": 100, "gain": 1 }
      },
      "forAcquisition": true
    }
  },
  "lasers": {
    "561 nm": {
      "analogChannel": null,
      "digitalLine": null,
      "managerName": "Cobolt0601NewLaserManager",
      "managerProperties": {
        "digitalPorts": ["COM4"]
      },
      "wavelength": 561,
      "valueRangeMin": 0,
      "valueRangeMax": 200
    }
  },
  "availableWidgets": [
    "Settings",
    "View",
    "Recording",
    "Image",
    "Laser"
  ]
}
```

`availableWidgets` controls which UI panels are shown. Add `"Positioner"` and `"Scan"` once you have a stage and DAQ configured.

---

## Project Structure

```
ImSwitch2/
├── imswitch/                     # Main package
│   ├── imcontrol/                # Hardware control module
│   │   ├── model/managers/       # One manager class per device type
│   │   └── _data/user_defaults/  # Example setup files (copied to ImSwitchConfig on first run)
│   ├── imcommon/                 # Shared framework (signals, Qt layer, logging)
│   ├── imreconstruct/            # SIM reconstruction module
│   └── imscripting/              # Scripting console module
├── utility_scripts/              # Config editor GUI + built-in device templates
├── docs/                         # Architecture documentation and diagrams
└── .github/workflows/            # CI pipeline
```

See [docs/design/ARCHITECTURE.md](docs/design/ARCHITECTURE.md) for a full breakdown of the manager system, controller hierarchy, and startup flow.

---

## AI Agent Workflow

ImSwitch2 uses AI agents as development assistants under strict human oversight:

```
GitHub Issue → Agent plans → Isolated branch → Tests → PR → Human review → Merge
```

- All agent code changes require human review — no automatic merges.
- Red-zone files (hardware timing, laser control, DAQ) require explicit maintainer approval.
- See [AGENTS.md](AGENTS.md) for full rules.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.
