# Tiling: stage-tilt calibration and compensation

The Tiling widget can measure an XY-dependent focus surface and later use its
slopes to move a selected Z positioner during **free-running** tiling. This is
**relative feed-forward**, not autofocus or verified focus recovery. The feature
does not affect non-tiling stage movements or triggered scanning.

## Record a calibration

1. In **Focus Lock**, engage the feedback loop on the Z positioner you intend
   to use for tilt compensation. To switch actuators *during a session*, unlock
   focus first, then click the small gear beside **See calib** and choose
   **Change focus Z actuator…**. Only configured Z-capable positioning devices
   are listed, including devices hidden from the normal Positioner widget.
   Switching itself never moves Z and is refused during scans, tiling, focus
   calibration, or when feedback is engaged. The selection is session-only;
   restart returns to the hardware setup's configured default. Check PI gains
   and repeat any actuator-specific calibration after switching.
2. In **Tiling**, select **Free-running**, your desired XY region, and your
   usual settle time. In the dedicated **Stage tilt** section choose **Record**.
   Optionally enter a calibration **Name**; the current Focus Lock **Z actuator**
   appears on the same line, read-only. The additional Record options are:

   - **Calibration only** (default ON): no tile-camera activation, imaging,
     saving, registration or mosaic. XY and the Focus Lock camera/PI still run.
   - **Bidirectional** (default OFF): traverse the same XY points forward, then
     *reverse the complete visit order from the last tile*. No jump back to
     the starting corner between passes. Reverse readings are paired by XY
     position and averaged for the fitted plane. Image acquisition (when
     Calibration only is OFF) happens only on the forward pass.
   - **Samples X × Y** (default 5 × 5): enabled in Calibration only mode.
     These counts specify sampling **density**, not the area. The full area
     remains defined by Scan's Tiles X/Y and Step. For example, 21 × 21 with
     20 µm steps spans 400 × 400 µm; sampling it at 5 × 5 measures only 25
     positions per pass, all the way to its boundaries. Serpentine extends
     from the starting corner; spiral stays centred on the starting position.
     With Calibration only OFF, the full imaging grid is sampled instead.

   Tiling waits the existing settle time and samples the focus controller's
   current actuator's reported Z at each calibration position. This is
   physical readback for Leica and software-tracked commanded Z for
   NI-DAQ/Serial DAC.
3. Completed (or interrupted) calibration files are saved under
   `ImSwitchConfig/imcontrol/tilt_calibrations/calibration_<timestamp>_<name>/`
   when a name is provided (or `calibration_<timestamp>/` otherwise):
   `tilt_measurements.csv`, `tilt_fit.json`, and, if a plane is fitted,
   `tilt_fit.png`. The original snapshot metadata is preserved, including the
   actual Z positioner name (`focus_positioner`) and XY positioner name.
   Each CSV reading identifies its pass (1 or 2); invalid/unlocked readings
   remain in the CSV but are excluded from fitting. With two passes, both
   readings must be valid for a location to contribute to the fit, and the
   PNG adds a reverse-minus-forward Z difference map.

   **Automatic end-of-run drift diagnostic:** after Tiling has requested the
   XY return to the initial position and waited the usual settle time, it
   takes one more Focus Lock Z reading. `tilt_fit.json` records initial Z,
   final Z and their difference in `origin_drift` (also shown in **See
   calibration**). The origin measurements never affect the fit. If the
   return command fails or a Focus Lock reading is unusable, drift is
   unavailable, not zero. A requested stage return is not proof of physical
   positioning, and the reported focus Z may be hardware readback or an
   open-loop manager's last commanded Z.
4. A completed valid calibration is selected automatically. The **Record**
   row also gains a `…` menu for the **most recently recorded calibration**;
   **See calibration** and **Open calibration folder** work directly from
   Record mode, without switching to Apply or choosing the JSON file. The
   menu also offers **Set as default** for that recording. The Apply-mode
   menu continues to act on the currently selected calibration. To load an older
   calibration (including a version-1 file from a previous tiling folder),
   choose **Apply** and click the displayed calibration name (or **Choose
   calibration…**) to select its `tilt_fit.json`.

For approximately linear Z drift and similar timing in both directions,
forward/reverse averaging reduces spatial tilt bias. With **Calibration only**
OFF, tile images take time on the forward pass but not on the reverse pass;
the two passes are not exactly time-symmetric, so drift cancellation is only
approximate. For controlled comparisons of sampling density and bidirectionality,
prefer **Calibration only** ON. Dense sampling also helps reveal deviations
from a plane (for example local sample curvature) in the saved residual map;
it does not force a curved surface into the model.

The **selected** calibration is used in the current session. Only **Set as default** in the `…` menu remembers that calibration for the current hardware
setup in `config/tiling_tilt_preferences.json`; the default is loaded when the
Tiling widget is next initialized. Newly recorded or manually loaded calibrations
do **not** replace it automatically. The Stage tilt **Mode** always starts **Off**.
Calibrations represent stage *and sample* geometry and may not remain valid after
sample remounting.

Custom names are stored in the adjacent `tilt_metadata.json` file. A
Windows-safe, shortened snapshot of the name is also appended to the folder
name **at recording time** to make browsing easier. Renaming a calibration
in **See calibration → Rename…** changes the display name only; it never moves
the existing folder or modifies `tilt_fit.json` or its SHA-256 identity. Older unnamed files display their recording date instead.
**See calibration** opens a resizable dialog using the already-saved `tilt_fit.png`
plus fit statistics and metadata; if the PNG is absent, the metadata remains
available. **Open calibration folder** opens the selected file's parent folder,
including calibrations loaded from outside the workspace.

## Apply the calibration

1. Focus at the first tile. Choose **Apply** in the Stage tilt section.
   The imported JSON identifies the compensation Z actuator; the active
   hardware setup must contain that exact Z-capable positioner. No separate
   actuator selector is needed. For example, a calibration recorded using `Z`
   drives NI-DAQ `Z`, while focus lock may simultaneously stabilize on
   `FocusPiezoDAC`.
2. If both features target **the same named Z actuator**, unlock focus first.
   Using different named actuators permits simultaneous operation, provided
   they truly drive independent hardware. Tiling does not pause or restart the
   PI when moving XY. Verify the transient response experimentally.
3. Optionally enable **Pre-check** (tooltip: tilt compensation). This checks the entire
   intended XY route against the calibration bounds, all predicted Z targets
   against supported DAC/NI-DAQ voltage limits, actuator availability, and
   reference status **before movement**. It starts **unchecked** for testing.
   Unchecked means no full-route limit/extrapolation check; finite-value,
   device-availability and reported command errors still apply. Do not
   rely on unchecked mode for unattended or untested travel ranges.
4. Each tile move commands XY, then requests an **absolute Z target**:
   `Z_target = Z_start + slope_x * (X-X_start) + slope_y * (Y-Y_start)`.
   Tiling then waits your existing settle interval and acquires. The saved
   calibration's absolute `z0` is **never** commanded: the starting Z is read
   from the selected positioner when the run begins. Z does not need to reach
   the target before the command returns (for example, Leica objective Z).
5. On completion or cancellation, tiling unwinds its XY moves and requests
   the original absolute Z, provided XY restoration succeeded and there is no
   focus-lock actuator conflict. It does **not** verify physical Z arrival.
   Reported command errors and incomplete restoration requests are logged.

During compensated tiling, regardless of **Save tiles**, the normal tiling output
folder contains:

- `tilt_compensation.csv`: XY, predicted and commanded *compensation* Z,
  optional focus-camera signal relative to the first tile, and validity.
- `tilt_validation.json`: calibration identity, coefficients, run completion,
  and focus-signal statistics where available.
- `tilt_validation.png`: focus-signal comparison when enough valid readings
  are available. No fine-focus DAC position history is recorded.

Focus-camera monitoring is optional for this feed-forward experiment. When focus
is locked on a *different* actuator, the camera trace describes the residual
signal after PI correction—not an independent open-loop focus error.

## Limits

The Z origin uses `get_abs()`, which is **physical position readback** for
Leica and a **software-tracked commanded position** for NI-DAQ/Serial DAC.
Subsequent Z values in the compensation CSV are **requested targets**, not
confirmed physical positions. An accepted `setPosition()` command does not
prove that Leica has arrived, or that an open-loop piezo actually moved.
Ensure the existing settle time is sufficient for the selected actuator.
The optional pre-check is a software guard, not collision prevention or proof
of focus. There is no automatic focus reacquisition or focus-readiness gate
for acquisition yet.
