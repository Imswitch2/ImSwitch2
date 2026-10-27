# Optional Fiji integration for ImProcess: architecture assessment and plan

Branch `feat/improcess-fiji`. The P0 prototype (section 8) was a throwaway outside the repo; its fixes are carried into `imswitch/improcess/fiji/` from P1 on.

2026-10-26. Assessment (sections 1–7) and prototype results (section 8).
Tags: **[V]** means verified in this repo or in the upstream source/docs. **[I]** means inferred and still needs the prototype.

---

## 1. What ImProcess already has

| Concern | Where | Fit for Fiji |
|---|---|---|
| Drop-in plugins | `improcess/plugins/user_plugins.py`, `processors/__init__.py` | **Wrong shape.** A `Processor` is a synchronous `apply(result) -> result` with a param panel. It has no session, no long-lived background resource, and no "results come back later". [V] |
| **napari endpoints** (send a result to an external tool, take outputs back) | `model/napari_endpoints.py`, `model/napari_sessions.py`, `model/napari_import.py`, `controller/NapariEndpointController.py`, *Plugins → napari plugins* submenu | **Right shape, and the closest prior art.** It already solves everything a Fiji link needs: a session owns what a send created (temp dir, worker, outputs); export runs off the GUI thread on a QThread; nothing comes back automatically, the user explicitly imports an output against a chosen source result; a grid decision (`grid_decision`) gives an output a fresh coordinate space unless shape, scale, units, axes and transform all prove it is the same grid; and `record_import` writes a non-replayable provenance node. [V] |
| Image model | `model/result.py` `ProcessingResult` / `ArrayProcessingResult` | `data` + `axis_labels` + `axis_scales` + a single `scale_unit` + `metadata`, with stable `result_uid` / `dataset_uid` / `coordinate_space_uid` / `lineage`. Fiji outputs become `ArrayProcessingResult` / `LabelsResult`. No second image model is needed. [V] |
| Registering outputs | `commChannel.sigResultProduced.emit(result, name)` | This is the same path the napari import uses. [V] |
| OME description | `imcommon/model/ome_metadata.py` `OmeImageMeta`; `result_io.ome_meta_for_result` | Gives axes, physical sizes, channel names and annotations to map onto ImageJ `Calibration` / slice labels. [V] |
| Provenance | `model/provenance.py` (DAG under `metadata["provenance"]`); `model/footprint.py` (linear history derived from the DAG) | Ops are `source, reconstruct, consolidate, process, napari-import, opaque`. `make_node` and `validate_graph` reject unknown ops. [V] |
| Optional deps | `setup.cfg` extras (`imagej` = roifile, `comet`, `storm` …), imported lazily with a disabled UI and an install hint | A new `fiji` extra follows the same pattern. [V] |
| Process model | `python -m imswitch` registers imcontrol + improcess + imscripting in **one process and one QApplication** | This matters: an embedded JVM would live inside the microscope-control process. [V] |

**Verdict.** The *processor* plugin interface is not suitable, and stretching it would be wrong. The *endpoint/session/import* design is suitable as a pattern, and most of its pieces can be reused unchanged. Two parts are napari-specific: `LayerSnapshot` is described as a napari layer, and the provenance op name is `napari-import`.

**Smallest general change in core:**
1. Provenance: generalise `record_import` to take a `tool` (`"napari"`, `"fiji"`). Either add one op `external-import` (keep reading `napari-import` for old files), or keep the op name and add a `tool` field. This is about 20 lines plus a test.
2. Nothing else. `import_layer(snapshot, source_result, plugin_name=..., preserves_grid=False)` already takes a plain dataclass, so a Fiji image can be described as a `LayerSnapshot` (data, scale, translate from the calibration origin, units, metadata with `result_uid` / `axis_labels`). The grid, unit and provenance logic then applies as it is.

All Fiji code lives in its own subpackage `imswitch/improcess/fiji/`. It is imported lazily and nothing in core imports it.

---

## 2. PyImageJ / Fiji facts that decide the architecture

- pyimagej 1.8.0, scyjava 1.12.5, jpype1 1.7.1, Python ≥3.10 on main. Modes are `headless`, `gui` and `interactive`. [V]
- **macOS:** `interactive` mode raises `OSError` when called on the main thread from plain Python. It is supported only under IPython `%gui osx` or the Jaunch launcher. `gui` mode runs the AppKit loop on the main thread forever (`jpype.setupGuiEnvironment`, needs PyObjC) and runs our callback on another thread. napari-imagej, the in-process Qt prior art, **disables the ImageJ GUI on macOS entirely**. [V]
- **One JVM per process, never restartable.** The JVM uses SIGSEGV internally, so Python's `faulthandler` must be off. In GUI mode with legacy enabled, closing ImageJ can terminate Python. Exit hangs are a known issue. [V]
- numpy → Java shares memory through imglyb. Java → numpy always copies. Axis mapping: X↔col, Y↔row, Z↔pln, C↔ch, T↔t, and arrays are reversed (C- vs F-order). **Units are never transferred** in either direction. Calibration crosses only as xarray coords → linear axes. [V]
- `ImagePlus.getID()` gives unique negative per-session ids, and `WindowManager.getIDList()` / `getImage(id)` exist. When headless, WindowManager is empty and images cannot be enumerated. [V]
- **Fiji Latest** (2026) is laid out as `Fiji/{jars,plugins,java}`, ships Zulu **21** arm64/x64 and ij 1.54p. By default scyjava downloads a **Zulu 11** JRE, which very likely cannot load Fiji Latest's Java 21 classes. So a configuration needs `fetch='never'` + `JAVA_HOME` = Fiji's bundled JDK. imglyb pulls `imglib2-imglyb` from Maven at first start, so the first start is **not offline**. [V for the facts, I for the clash]
- **This Mac:** `~/software/Fiji.app` is a 2023 Fiji (ij 1.54f) with a **Java 8 x86_64** JRE. There is no system Java, Python is arm64, and pyimagej is not installed. That Fiji is unusable from this Python. [V]
- A running Fiji can be reached from outside only one way, through the ImageJ1 RMI single-instance listener, which accepts `eval <macro>` and returns no result. So "attach to the user's own Fiji" is not viable, which matches the brief. [V]

---

## 3. Embedded or separate process

| | A. Embedded PyImageJ | B. Fiji host process (recommended) |
|---|---|---|
| Fiji GUI on macOS | Unsupported. The only route is untested: call `init('interactive')` from a QThread and hope Qt's NSApplication loop serves AWT. | Supported: the child runs `init(mode='gui')`, AppKit owns the child's main thread, and the IPC server runs in the callback thread. **[I] confirm in the prototype.** |
| Fiji quits or crashes | Python can die with it, and the JVM can never restart in this process. | The child dies, ImProcess reports it, and *Start Fiji* spawns a new one. |
| Risk to microscope control | The JVM shares a process with imcontrol hardware threads. Its SIGSEGV games interfere with faulthandler. Exit is fragile on top of the existing PyQt5 teardown segfault history. | None: separate address space. |
| Data transfer | Zero-copy to Java, but Fiji can then **mutate ImProcess's array in place**, which needs a defensive copy anyway. | One copy each way. That copy is also what guarantees Fiji never touches the original. |
| Complexity | Less IPC, but every Java call needs thread attachment and Qt marshalling. | A small request/response protocol plus a lifecycle (spawn, handshake, heartbeat, terminate). |

**Recommendation: B.** It is not the more complex choice for its own sake. In-process GUI on macOS is officially unsupported, and ImProcess shares its process with hardware control. The usual IPC cost is mostly a copy that A needs anyway to protect the original.

**IPC, kept simple:**
- **Control:** `multiprocessing.connection` (localhost, random `authkey`, pickled small dicts). It is in the standard library, with no new dependency.
- **Pixels:** `.npy` files in the session temp dir (ImProcess already has `SessionRegistry.temp_dir_for`). The host reads them with `np.load(mmap_mode='r')` and copies into an ImagePlus. Return images come back the same way. A crash leaves only files in a dir the session deletes. Move to `multiprocessing.shared_memory` only if the large-image measurements show the SSD path is the bottleneck. **[I] measure.**
- **Child interpreter:** `sys.executable -m imswitch.improcess.fiji.host`, in the same env as ImSwitch with the `fiji` extra (`pyimagej`, `pyobjc-framework-Cocoa` on macOS). The child never imports Qt or the rest of ImSwitch.

---

## 4. Proposed plugin

**Package `imswitch/improcess/fiji/`:**
- `protocol.py`: message dataclasses and the request/response helper. Qt-free.
- `convert.py`: a `ProcessingResult` → hyperstack plan, and a Fiji image description → `LayerSnapshot`. Pure numpy, testable without Java.
- `host.py`: the child process. It runs `imagej.init(fiji_dir, mode='gui')`, then serves `send`, `list`, `fetch`, `history` and `ping`. On UI show it calls `exitWhenQuitting(False)` and removes the window listeners, like napari-imagej. The only file that imports `imagej`/`jpype`.
- `session.py`: spawns, handshakes, sends heartbeats and tracks state (`stopped/starting/ready/failed/dead`). Qt-free.
- `controller/FijiController.py`: Qt glue. Every request runs on a worker QThread and the result returns by signal.

**UI:** a *Plugins → Fiji* submenu with *Start Fiji*, *Send to Fiji* (current result), *Import from Fiji…* (a dialog listing open images: title, ID, shape, origin = sent / derived from <name> / unknown), *Import new images*, *Stop Fiji*, and *Fiji location…* (persisted via `FolderPreferences`). Every entry is disabled with an install hint when the extra or Fiji is missing.

**Send mapping (explicit, never guessed):**
- `axis_labels` must map onto ImageJ's XYCZT.
- Any other axis (`Dataset`, `Base`, `Angle`, …) is **refused** with a message to project or split first. A later step could offer "fold axis into C/T".
- `axis_scales` + `scale_unit` → `Calibration` (`pixelWidth/Height/Depth`, `frameInterval`, units set explicitly because pyimagej does not).
- Channel names → slice labels.
- `result_uid` and `axis_labels` → ImagePlus properties.
- The original OME metadata **stays on the ImProcess side** in the session map, keyed by the ImagePlus id. Fiji is not trusted to carry it.

**Import mapping:**
- Fiji's `Calibration` → `LayerSnapshot`, then the existing `import_layer`.
- A crop or resample gets a fresh grid automatically, because shape or scale differs. A uint8/float32 conversion keeps the grid only if everything else matches.
- Any metadata that cannot be verified is not copied. It is flagged in `metadata["fiji"]["dropped"]`.

---

## 5. Provenance (the addendum)

Best effort, assembled from hooks Fiji already exposes. No instrumentation framework.

- `Executer.addCommandListener` gives the command name before it runs [V]. At that moment `WindowManager.getCurrentImage().getID()` is the input [I]. `Macro.getOptions()` holds the options for macro/IJ.run calls, but not for dialog-driven menu use [I].
- With the GUI up, a hidden `Recorder(false)` is possible. After each command, `Recorder.getCommandOptions()` / the recorder text gives the macro line *with* dialog parameters [V that the API exists; I that it can be read per command]. This is the most useful source of parameters.
- `ImagePlus.addImageListener` gives `imageOpened`, `imageUpdated` and `imageClosed`, dispatched on the EDT [V]. An `imageOpened` while a command is running links output to input. An `imageUpdated` with no surrounding command marks the image "modified outside a recorded command" (paintbrush, ROI fill, script Java calls).
- SciJava `ModuleExecutedEvent` through the EventService (the napari-imagej "HACK" subscriber) covers ImageJ2 commands. **Defer** this until the IJ1 hooks prove insufficient.
- **Stored as:** one provenance node per imported image (the generalised import op, `tool: "fiji"`, `replayable: false`) carrying `fiji_ops: [{command, options, input_id, output_id, completeness: "recorded" | "partial" | "unknown"}]`, plus Fiji/ij versions. The linear history then shows "Imported from Fiji" followed by the readable op list.

---

## 6. Open questions only the prototype answers

1. Does `init(mode='gui')` in a child process on macOS arm64 really serve IPC from the callback thread with the GUI fully interactive? This is the critical unknown.
2. Does pyimagej run against Fiji Latest's bundled Java 21 with `fetch='never'`? What is needed offline (the imglyb jar)?
3. Do the ImagePlus properties (`result_uid`) survive *Duplicate*, *Crop* and *Z Project*? Does a crop update `Calibration.xOrigin`?
4. Can Recorder options be read per command, and does the CommandListener's current image reliably identify the input?
5. What is the transfer time and peak RSS for a ~2 GB stack through `.npy` (copy count on each side)?
6. Windows: is the Fiji Latest JDK path plus the jpype DLL setup clean? Linux/X11 is lowest risk. Both need a rig or VM check later.

## 7. Plan

- **P0 prototype**, in a throwaway venv outside the repo: answer 1–5 with the test matrix in the brief.
- **P1** `convert.py` + `protocol.py` + the provenance generalisation, with unit tests (no Java).
- **P2** `host.py` + `session.py`, with tests: a fake host for the lifecycle tests, and real-Fiji tests marked `fiji` and skipped when it is absent.
- **P3** controller + menu + import dialog.
- **P4** provenance tracker.
- **P5** docs (`docs/improcess.rst` section: install, Fiji Latest, `JAVA_HOME`, supported metadata, limitations).

Everything goes on one branch.

---

## 8. P0 prototype results (2026-10-26, macOS arm64, 8 GB)

**Setup:**
- Fiji Latest arm64 (ImageJ 2.18.0 / ij 1.54p, Zulu 21.0.7), throwaway venv with pyimagej 1.8.0 / scyjava 1.12.5 / jpype 1.7.1.
- Parent: plain CPython; child: `proto/host.py` with `mode='gui'`.
- Code and raw results were kept outside the repo (throwaway).

**Open questions from section 6, answered:**

1. **macOS GUI in a child process: WORKS.**
   - `init(mode='gui')` runs AppKit on the child's main thread, and the IPC server runs on a Python thread started from the `when_imagej_starts` callback.
   - Fiji is non-headless, and image windows open on screen.
   - **A user drove the GUI by hand** (Duplicate, Gaussian Blur, rectangle + Crop), and the hooks recorded all of it.
   - The child appears as a foreground app with no bundle id, so a later step should give it a name and icon.

2. **Java:**
   - **pyimagej 1.8.0 bug:** `fetch='never'` crashes in `_guess_java_version` (unbound `major_version`). Workaround: `fetch='auto'` + `JAVA_HOME` = Fiji's bundled JDK; Java 21 was confirmed in use.
   - **Heap:** without `-Xmx` the JVM takes 25% of RAM, and a 2 GB stack hit `OutOfMemoryError`. The host now applies Fiji's own rule (`config/jaunch/fiji.toml: cfg.max-heap = '75%'`).
   - **Offline:** pyimagej refuses to start without `imglib2-imglyb` on the classpath, even though the IJ1 path never uses it. By default jgo resolves it from Maven (first start 92 s, 38 jars, including **imglib2 5.12.0 next to Fiji's 8.0.0**, a version skew). Fix: clear `scyjava.config.endpoints` and `add_classpath` only `imglib2-imglyb-1.1.0.jar` + `imglib2-unsafe-1.0.0.jar`. That gives a **7.2 s fully offline start with no skew**. These two jars must ship with the plugin or be fetched once.

3. **Properties are a lineage hint, not an identity:**
   - Duplicate, Crop and Scale copy `imswitch.result_uid` onto the derived image; Z Project drops it.
   - Identity must come from the ImagePlus ID plus the host's own record of what was sent.
   - **Crop never updates `Calibration.xOrigin/yOrigin`**, scripted or by hand. The offset exists only in the recorded `makeRectangle(...)`, so a crop always imports on a fresh grid.
   - Scale updates `pixelWidth` correctly.
   - Duplicate from the GUI defaults to the current slice only (5-slice source → 1 plane).

4. **Provenance hooks:**
   - **CommandListener:** gives the command name and input image ID for menu and script use alike. `Macro.getOptions()` has parameters only for scripted `IJ.run`; it is `None` from the menus.
   - **Hidden `Recorder(false)`:** captures GUI parameters (`run("Gaussian Blur...", "sigma=2")`, `makeRectangle(76, 100, 94, 80)`) but **not** scripted `IJ.run` calls. The two sources are complementary.
   - **ImageListener:** `imageOpened` identifies the output of Duplicate, Z Project and Scale. In-place ops (8-bit, Blur, Crop) keep the ID and fire `imageUpdated`.
   - Recorder lines reference images by **title**, so they are correlated to command events by order.
   - Listeners fire on the EDT or the command thread; the host only appends to a locked list.

5. **Transfer** (copy each way; the original is never touched: `original_untouched` was true in every case):

   | Size | Send | Fetch |
   |---|---|---|
   | small (≤5D, 2 KB–200 KB) | 60–100 ms round trip | |
   | 256×256 u16, ×20 | median 0.107 s (send + fetch + close) | |
   | 512 MB | 3.7 s | 0.8 s |
   | 1 GB on this 8 GB Mac | 19 s | 37 s |

   The 1 GB case is clearly swapping (three copies alive across two processes). Fix later: a plane-wise memmap write (`open_memmap`) in the host and a lower peak in the parent.

6. Not tested: Windows and Linux.

**Test matrix, all PASS:**
- dtypes and shapes: 2D uint8 / uint16 full range / float32 with negatives + NaN; ZYX; CYX with channel names → slice labels; TYX with frame interval; 5D TZCYX; nontrivial order XZCY; non-contiguous view input.
- refusals: an extra axis (`Base`) and float64 are refused with a clear message.
- image handling: duplicate, crop, Z project, 8-bit in place, scale; close + reopen with the same title (IDs are never reused, and fetching a closed ID errors).
- robustness: 20 repeated transfers; killing Fiji mid-session is detected (`host is gone (exit -9)`); the temp dir is empty afterwards.
- Unit text comes back normalised (`um` → `µm`, default time unit `sec`), so the import side must map units back.

**Design changes from the prototype:**
- Bundle or fetch the two imglyb jars and never let jgo resolve.
- Set `-Xmx` from Fiji's 75% rule, with a user override.
- `fetch='auto'` until pyimagej fixes `fetch='never'`.
- Provenance = CommandListener (command + input ID) ⊕ Recorder (GUI params) ⊕ `Macro.getOptions` (scripted params) ⊕ ImageListener (output IDs), correlated by order. Mark a step `recorded` when parameters were captured, `partial` when only the command name was, and `unknown` for an `imageUpdated` with no surrounding command.
- Crop offsets are taken from the recorded `makeRectangle` and stored as context only; the imported result still gets a fresh grid.
