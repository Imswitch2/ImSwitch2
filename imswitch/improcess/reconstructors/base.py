"""Base contract for ImProcess reconstructor plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import threading
from typing import Any, Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model.acquisition_layout import (
    REGISTERED_PAYLOAD_KINDS,
    LayoutIssue,
)
from imswitch.improcess.model import DataObj
from imswitch.improcess.model.acquisition_layout_resolver import (
    AcquisitionLayoutResolutionError,
    ResolvedAcquisitionLayout,
)
from imswitch.improcess.model.result import ProcessingResult, ViewMode


@dataclass(frozen=True)
class AcquisitionRequirements:
    """Declarative acquisition semantics required by one reconstructor."""

    payload_kinds: frozenset[str]
    required_loop_kinds: frozenset[str] = frozenset()
    allowed_extra_loops: str = "reject"
    requires_calibrated_loops: frozenset[str] = frozenset()
    allow_ambiguous: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload_kinds", frozenset(self.payload_kinds))
        object.__setattr__(self, "required_loop_kinds", frozenset(self.required_loop_kinds))
        object.__setattr__(
            self,
            "requires_calibrated_loops",
            frozenset(self.requires_calibrated_loops),
        )
        if not self.payload_kinds:
            raise ValueError("AcquisitionRequirements.payload_kinds cannot be empty")
        if self.allowed_extra_loops not in {"reject", "select", "split", "reduce"}:
            raise ValueError(
                "allowed_extra_loops must be reject, select, split, or reduce"
            )


class AcquisitionPreflightError(ValueError):
    """A strict reconstructor cannot safely consume the resolved acquisition."""

    def __init__(self, message: str, issues: tuple[LayoutIssue, ...]) -> None:
        super().__init__(message)
        self.issues = issues


def preflight_acquisition_layout(
    resolved: ResolvedAcquisitionLayout,
    requirements: AcquisitionRequirements,
) -> tuple[LayoutIssue, ...]:
    """Compare one resolved layout with plugin-declared requirements."""
    issues = list(resolved.issues)
    layout = resolved.layout
    if layout.payload_kind not in requirements.payload_kinds:
        issues.append(
            LayoutIssue(
                "error",
                "UNSUPPORTED_ACQUISITION_PAYLOAD",
                f"This reconstructor accepts {sorted(requirements.payload_kinds)}, "
                f"not {layout.payload_kind!r}",
                "payload_kind",
            )
        )

    if layout.payload_kind not in REGISTERED_PAYLOAD_KINDS:
        issues.append(
            LayoutIssue(
                "error",
                "UNVALIDATED_ACQUISITION_PAYLOAD",
                f"Payload kind {layout.payload_kind!r} has no registered strict validator",
                "payload_kind",
            )
        )

    incomplete_codes = {"FRAME_COUNT_MISMATCH", "LOOP_AXIS_COUNT_MISMATCH"}
    if any(issue.code in incomplete_codes for issue in resolved.issues):
        issues.append(
            LayoutIssue(
                "error",
                "INCOMPLETE_ACQUISITION_LAYOUT",
                "The stored data does not contain the complete declared acquisition layout",
                "shape",
            )
        )

    loop_kinds = {loop.kind for loop in layout.event_loops}
    missing = requirements.required_loop_kinds - loop_kinds
    for kind in sorted(missing):
        issues.append(
            LayoutIssue(
                "error",
                "MISSING_REQUIRED_ACQUISITION_LOOP",
                f"Required acquisition loop {kind!r} is missing",
                "event_loops",
                kind,
            )
        )

    if requirements.allowed_extra_loops == "reject":
        for kind in sorted(loop_kinds - requirements.required_loop_kinds):
            issues.append(
                LayoutIssue(
                    "error",
                    "UNSUPPORTED_EXTRA_ACQUISITION_LOOP",
                    f"Acquisition loop {kind!r} requires a selection or a compatible reconstructor",
                    "event_loops",
                    kind,
                )
            )

    for kind in sorted(requirements.requires_calibrated_loops):
        matching = [loop for loop in layout.event_loops if loop.kind == kind]
        if not matching:
            continue
        if any(loop.step is None or loop.unit is None for loop in matching):
            issues.append(
                LayoutIssue(
                    "error",
                    "UNCALIBRATED_ACQUISITION_LOOP",
                    f"Acquisition loop {kind!r} requires a physical step and unit",
                    "event_loops",
                    kind,
                )
            )

    if not requirements.allow_ambiguous and not resolved.is_usable:
        issues.append(
            LayoutIssue(
                "error",
                "AMBIGUOUS_ACQUISITION_LAYOUT",
                "Acquisition semantics are low-confidence; choose or persist a layout override",
                "AcquisitionLayout",
            )
        )
    return tuple(issues)


def recording_file_refusal(reconstructor) -> str:
    """The one message for a file-reading reconstructor handed a result."""
    from imswitch.imcommon.model.workflow.steps import recording_file_refusal as _refusal

    return _refusal(reconstructor)


def params_from_attrs(reconstructor, data_obj, params: dict | None, keys) -> dict:
    """``params`` with each of ``keys`` taken from the input's attributes
    where the parameter still holds the reconstructor's default.

    A result made upstream knows things a recording's widget defaults do not
    (a deskew writes the voxel size it resampled to as ``pixel_size_nm``);
    an explicit, non-default parameter still wins. The comparison is with the
    default because ``prepare_params`` is handed the merged defaults and step
    parameters and cannot tell them apart otherwise.
    """
    params = dict(params or {})
    try:
        attrs = dict(getattr(data_obj, "attrs", None) or {})
    except Exception:
        return params
    defaults = type(reconstructor).default_params()
    for key in keys:
        if key in attrs and key in defaults and params.get(key, defaults[key]) == defaults[key]:
            value = attrs[key]
            try:
                params[key] = type(defaults[key])(value) if defaults[key] is not None else value
            except (TypeError, ValueError):
                continue
    return params


@dataclass(frozen=True)
class DetectionPreview:
    """What :meth:`Reconstructor.detection_preview` draws: points and a line.

    ``x`` and ``y`` are column and row coordinates in the raw frame, so
    that a spot at pixel ``(row, col)`` lands on that pixel on screen.
    """

    x: np.ndarray
    y: np.ndarray
    status: str = ""
    marker: str = "circle"
    """How the points are drawn: ``"circle"`` (an open ring around each,
    for candidates to inspect) or ``"point"`` (a dot on each, as the classic
    pattern overlay draws its grid)."""


class Reconstructor(ABC):
    """
    Turns raw DataObj into a ProcessingResult.
    
    Each dataset is processed by exactly one reconstructor. Examples:
    - MoNaLISA SIM pattern extraction + reassignment
    - SNOUTY lightsheet deskew
    - View-only (wraps raw data with no processing)
    - STED deconvolution
    """
    
    # Class attributes (override in subclasses)
    name: str = "Unnamed Reconstructor"  # Human-readable, shown in plugin picker
    id: str = "unnamed"  # Stable identifier for config + registry lookups
    file_extensions: list[str] = ["hdf5", "tiff", "zarr"]  # Watcher dispatch + filtering
    accepted_source_kinds: tuple[str, ...] = ("image",)
    execution_policy: str = "inline"
    is_pass_through: bool = False
    """Set ``True`` for reconstructors whose ``process()`` is a no-op wrap.

    When the active reconstructor is pass-through, ImProcess auto-routes the
    current ``DataObj`` to the napari viewer the moment it changes, so the
    user does not have to click 'Reconstruct current' for a plugin whose only
    job is to display the data. Pass-through plugins also hide the
    'Reconstruct current' and 'Update reconstruction' actions, since those
    are ceremonial in their case.
    """

    default_save_subdir: str = "rec"
    """Subdirectory name a save path should use for reconstructed outputs.

    Declarative metadata only: the batch file watcher that consumed it was
    removed with the rest of the old watch-and-run path, so nothing reads it
    today. It is retained as part of the plugin contract for a future save
    path, which would write outputs under
    ``{output_dir}/{default_save_subdir}/``. Override per modality to pick a
    plugin-appropriate folder name (e.g. ``"deskew"`` for SNOUTY). ``"rec"``
    is the historical MoNaLISA default.
    """

    supports_consolidation: bool = False
    """Set ``True`` when :meth:`consolidate` can merge per-item results.

    The multidata action 'Consolidate into a single reconstruction' is only
    enabled for plugins that opt in; for the others the action stays visible
    but disabled, so a multidata run never silently degrades to individual
    processing.
    """

    acquisition_requirements: AcquisitionRequirements | None = None
    """Opt-in strict acquisition gate; ``None`` preserves legacy behaviour."""

    #: How this reconstructor treats a source that is one timepoint of a
    #: series (a timelapse recorded one file / one tiling folder per timepoint;
    #: see :mod:`imswitch.improcess.model.series`):
    #:
    #: * ``"default"`` -- the base class runs :meth:`process` once per timepoint
    #:   and stacks the results along a leading ``T`` axis. Nothing to write.
    #: * ``"own"`` -- the reconstructor handles the whole series itself:
    #:   implement :meth:`process_series` (the tiling mosaic does, to give every
    #:   timepoint one layout and one canvas).
    #: * ``"none"`` -- a series is not meaningful for it (it already takes a
    #:   whole lapse, or its result cannot be stacked); the series parameters are
    #:   ignored and it behaves exactly as it always did.
    series_policy: str = "default"
    #: What the series parameter's ``auto`` means for a source that is one
    #: timepoint of a series: ``"this"`` (the historic behaviour: process the
    #: opened timepoint) or ``"all"``. Opening the series' own manifest means
    #: ``all`` whatever this says.
    series_default: str = "this"

    @classmethod
    def supports_series(cls) -> bool:
        """Whether the series parameters apply to this reconstructor."""
        return getattr(cls, "series_policy", "default") != "none"

    @classmethod
    def series_param_spec(cls) -> tuple:
        """The reserved series parameters (``_series``...) as fields, or ``()``.

        Kept apart from :meth:`param_spec` on purpose: a plugin that knows
        nothing about series has a ``param_spec`` that must equal its
        ``default_params``. Forms and the workflow editor append these.
        """
        if not cls.supports_series():
            return ()
        from imswitch.improcess.model.series import series_param_fields

        return series_param_fields()

    def process_series(self, selection, data_obj, params: dict, context=None, *,
                       progress=None, cancel=None):
        """Reconstruct a whole series (``series_policy == "own"`` only).

        ``selection`` is a :class:`~imswitch.improcess.model.series.SeriesSelection`:
        the members to process, the ones left out and why, and the request.
        ``data_obj`` is the opened timepoint. Return one result; the runner
        records the provenance node with every member listed. ``progress(done,
        total, text)`` and ``cancel()`` are asked once per timepoint, and
        ``context`` carries the worker's progress, cancellation and memory
        budget as for :meth:`process`.
        """
        raise NotImplementedError(
            f"{self.name} declares series_policy='own' but does not implement process_series"
        )

    #: The kind of result a reconstruction produces, declared (see
    #: :attr:`~imswitch.improcess.processors.base.Processor.output_kind`).
    output_kind: str | None = None
    #: See :attr:`~imswitch.improcess.processors.base.Processor.deterministic`.
    deterministic: bool = True

    #: Version of this reconstructor's parameter contract; see
    #: :attr:`~imswitch.improcess.processors.base.Processor.params_version`.
    params_version: int = 1
    #: Keys of :meth:`default_params` whose widget default is machine-dependent.
    default_params_volatile: tuple[str, ...] = ()

    def output_spec(self, params: dict | None = None, input_specs=None):
        """The one port a reconstruction produces, ``out``, with its declared kind.

        Same shape as a processor's, so a workflow editor treats both alike.
        """
        from imswitch.improcess.model.result import UNKNOWN_KIND
        from imswitch.improcess.processors.base import OutputSpec

        return OutputSpec(ports=("out",), kind=self.output_kind or UNKNOWN_KIND)

    @classmethod
    def default_params(cls) -> dict:
        """The parameters a fresh widget would hand ``process``; pinned to the
        widget by a test."""
        return {}

    #: Parameter keys accepted beyond :meth:`default_params` (MoNaLISA's
    #: ``scan_params`` is filled from the file, not a widget). ``None`` means
    #: "anything".
    extra_param_keys: tuple[str, ...] | None = ()

    @classmethod
    def param_keys(cls) -> frozenset[str] | None:
        """Every parameter key a workflow may set, or ``None`` for unchecked."""
        if cls.extra_param_keys is None:
            return None
        # The reserved series options are settable on any step that supports
        # series, though they are not among its parameters.
        series = frozenset(field.key for field in cls.series_param_spec())
        return frozenset(cls.default_params()) | frozenset(cls.extra_param_keys) | series

    @classmethod
    def param_spec(cls) -> tuple:
        """Field-level description of :meth:`default_params`, one
        :class:`~imswitch.improcess.model.param_spec.ParamField` per key.

        What a form builder, the ``workflows list`` catalogue and the workflow
        editor read. The framework default derives each field from the
        default value's type (a number, a flag, free text, JSON), which is
        enough for a plain form; override it to say what the widget knows --
        the choices behind a combo box, the bounds and unit of a spin box,
        the tooltip -- with the same keys and the same defaults. A test pins
        the two together
        (:func:`~imswitch.improcess.model.param_spec.spec_problems`).
        """
        from imswitch.improcess.model.param_spec import spec_from_defaults

        return spec_from_defaults(cls.default_params())

    def encode_params(self, params: dict | None) -> tuple[dict, list[str]]:
        """``(encoded, reasons)``: params as lossless JSON, or why not."""
        from imswitch.improcess.model.provenance import encode_params

        return encode_params(params)

    def decode_params(self, encoded: dict | None, context=None) -> dict:
        """Inverse of :meth:`encode_params`."""
        from imswitch.improcess.model.provenance import decode_strict

        return dict(decode_strict(dict(encoded or {})))

    def migrate_params(self, encoded: dict | None, from_version: int) -> dict:
        """Bring params recorded under an older ``params_version`` up to date."""
        return dict(encoded or {})

    # -- declared input contract (all optional) -----------------------------

    core_dims_in: tuple[str, ...] | None = None
    """What one call of :meth:`process` consumes. **Every reconstructor
    declares it** (``test_reconstructor_contract`` checks the built-ins):

    * a tuple of axes -- the trailing axes of the input, in order; an entry
      is a label, several labels any of which will do (``"Z|Frame"``) or
      ``"*"`` (see :mod:`~imswitch.improcess.model.core_dims`). An input with
      more axes than these is **auto-indexed**: :meth:`process` is called once
      per index of the extra leading axes (T, C, a position), and the outputs
      are stacked with those axes in front (:meth:`stack_results`). An input
      of exactly this rank is passed as it is.
    * :data:`~imswitch.improcess.model.core_dims.WHOLE_INPUT` (``()``) --
      one call with the whole input, never looped (view-only, a localizer
      that consumes a whole movie).
    * ``None`` -- not declared: a plugin written before this contract. It is
      treated as ``WHOLE_INPUT`` so it keeps working; new plugins must not
      leave it ``None``.

    Whatever is declared, the run goes through
    :func:`~imswitch.improcess.reconstructors.run.run_reconstruction`, which
    records one provenance node (with ``iterated_over`` when it looped).
    """

    reads_recording_file: bool = False
    """``True`` when :meth:`process` needs the *recording file* itself, not
    just its pixels and attributes: a path, sibling files, a manifest, a
    measurement-run file. Such a reconstructor can only reconstruct a
    ``Source``; given a result (a reshape, another reconstruction) it is
    refused with the same message everywhere -- :meth:`check_input` before a
    run, workflow validation, and the editor's cables. ``False``: it reads
    ``data``/``data_handle``, ``axis_labels``, ``attrs``, and so takes a
    recording or any image result alike.
    """

    core_dims_out: tuple[str, ...] | None = None
    """The axes one call produces, for documentation and the editor; the
    stacked result takes its axes from what the calls actually returned."""

    required_attrs: tuple[str, ...] = ()
    """Attribute keys the input must carry unless a parameter of the same
    name gives the value (geometry a deskew cannot guess, say)."""

    def check_input(self, data_obj, params: dict | None) -> list[str]:
        """Why ``data_obj`` cannot be reconstructed with ``params``; empty when it can.

        Called before any pixel is processed (the runner, the GUI's
        reconstruct paths), and statically by workflow validation with
        ``data_obj=None``, when only the parameters are known. The default
        checks :attr:`core_dims_in` against the input's axes and
        :attr:`required_attrs` against its attributes; a plugin extends it
        (``super().check_input(...) + [...]``) for what only it knows.
        Messages, not exceptions: a caller shows all of them at once.
        """
        from imswitch.improcess.model.core_dims import CoreDimsError, match_core_dims, wants_iteration

        if data_obj is None:
            return []
        problems: list[str] = []
        params = dict(params or {})
        if self.reads_recording_file and getattr(data_obj, "provenance_parent", None) is not None:
            return [recording_file_refusal(self)]
        handle = getattr(data_obj, "data_handle", None)
        shape = tuple(getattr(handle, "shape", None) or getattr(getattr(data_obj, "data", None), "shape", ()) or ())
        # Guessed labels (a raw file that names no axes) decide nothing:
        # such an input is handed over whole, as before (iteration.plan).
        declared = bool(getattr(data_obj, "axis_labels_declared", True))
        if self.core_dims_in and wants_iteration(params) and shape and declared:
            try:
                match_core_dims(getattr(data_obj, "axis_labels", None) or [], shape, self.core_dims_in)
            except CoreDimsError as exc:
                problems.append(f"{self.name}: {exc}")
        if self.required_attrs:
            try:
                attrs = dict(getattr(data_obj, "attrs", None) or {})
            except Exception:
                attrs = {}
            missing = [key for key in self.required_attrs if key not in params and key not in attrs]
            if missing:
                problems.append(
                    f"{self.name} needs {', '.join(missing)}: neither the input's attributes "
                    "nor the parameters give it"
                )
        return problems

    def stack_results(self, results: list, match, *, loop_scales=None):
        """One result from the per-index results of an auto-indexed run.

        ``match`` is the :class:`~imswitch.improcess.model.core_dims.CoreMatch`
        that was looped over. The default stacks the arrays with the loop axes
        in front (:func:`~imswitch.improcess.model.iteration.stack_results`);
        override to return the plugin's own result type.
        """
        from imswitch.improcess.model.iteration import stack_results

        return stack_results(results, match, loop_scales=loop_scales)

    def prepare_params(self, data_obj, params: dict | None) -> dict:
        """Complete ``params`` from the data before a headless run.

        The GUI fills some parameters from the file behind the user's back
        (MoNaLISA's scan geometry comes from the acquisition attributes). A
        headless run has no controller to do that, so a reconstructor that
        needs it does it here. The default returns the params unchanged.
        """
        return dict(params or {})

    accepted_layouts: tuple[str, ...] | None = None
    """On-disk layouts the live path may hand this plugin, or ``None``.

    ``None`` means no restriction, which is the right default: most plugins
    care about the *shape* of the data, not how it was filed. The values are
    the ``LAYOUT_*`` constants in
    :mod:`~imswitch.improcess.live.source_type` -- named here as plain
    strings because that module reads sources, which read this one, and the
    dependency may only run one way.

    Consulted through :meth:`accepts_raw_source`; override that instead for
    anything this pair cannot express.
    """

    requires_frame_stacks: bool = False
    """Set ``True`` when a timepoint must be a *stack* of frames.

    A scanning reconstructor reassembles ``nx * ny`` raw frames into one
    image, so a recording contributing a single frame per timepoint gives it
    nothing to work with. This is independent of being a timelapse: a camera
    lapse is fifty timepoints of one frame each.
    """

    def accepts_raw_source(self, source_type) -> bool:
        """Whether this plugin can reconstruct a recording of this shape.

        Asked by the live path *before* a reader is built, so an unusable
        recording is skipped rather than opened and then abandoned.

        The default answers from :attr:`accepted_layouts` and
        :attr:`requires_frame_stacks`, which covers the declarative cases.
        Override for a constraint those cannot state -- but prefer declaring,
        so the answer stays inspectable without calling anything.

        Args:
            source_type: The recording's ``RawSourceType``.
        """
        layouts = self.accepted_layouts
        if layouts is not None and getattr(source_type, "layout", None) not in layouts:
            return False
        if (
            self.requires_frame_stacks
            and not getattr(source_type, "has_frame_stacks", False)
        ):
            return False
        return True

    @abstractmethod
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """
        Return the parameter editor widget embedded in ImProcessMainView left panel.
        
        Replaces the hard-coded ReconParTree. The widget should expose a
        `get_values() -> dict` method that the controller calls before processing.
        
        MoNaLISA example: pattern offsets/periods, PSF FWHM, BG model, denoiser name.
        View-only example: just a "Pixel size (nm)" input field.
        """
        ...
    
    @abstractmethod
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        Return the acquisition metadata dialog (replaces ScanParamsDialog).
        
        Return None if the reconstructor needs no acquisition metadata.
        
        MoNaLISA: 4D scan geometry (dimensions, directions, steps, unidirectional flag).
        View-only: None (no scan parameters needed).
        """
        ...
    
    @abstractmethod
    def process(
        self,
        data_obj: DataObj,
        params: dict,
        context: "ReconstructionContext | None" = None,
    ) -> ProcessingResult:
        """
        Run the reconstruction pipeline.
        
        Pure function over (data, params). No side effects, no GUI updates.
        All MoNaLISA-specific logic (PatternFinder, SignalExtractor, coeffsToImage)
        lives inside the MoNaLISA plugin's implementation of this method.
        
        Args:
            data_obj: Raw input data (HDF5/TIFF/Zarr)
            params: Parameter dict from `make_param_widget().get_values()`
        
        Returns:
            ProcessingResult with data + axis_labels + view_modes
        """
        ...

    def estimate_resources(
        self, data_obj: DataObj, params: dict
    ) -> "ResourceEstimate | None":
        """Return a cheap preflight estimate for GUI-thread confirmation."""
        return None
    
    def consolidate(self, results: list[ProcessingResult]) -> ProcessingResult:
        """Merge the per-item results of one multidata run into a single result.

        Called by the reconstruction manager after every item was processed
        individually with :meth:`process`; only invoked when
        ``supports_consolidation`` is True. Implementations should raise
        ``ValueError`` with a user-readable message when the results cannot be
        merged (e.g. differing scan geometry).
        """
        raise NotImplementedError(
            f'{self.name} does not support consolidated multi-data reconstruction'
        )

    def make_overlay(self, data_obj: DataObj, params: dict) -> Any | None:
        """
        Optional: provide a viewer overlay for the raw data view (DataFrame).
        
        Examples:
        - MoNaLISA: red scatter points showing detected SIM pattern grid
        - STED: depletion beam ROI outline
        - View-only: None (no overlay)
        
        Returns:
            A pyqtgraph GraphicsItem (e.g., ScatterPlotItem) to be added to
            DataFrame.imageItem.getViewBox(), or None for no overlay.
        """
        return None

    def detection_preview(
        self, data_obj: DataObj, displayed_image, params: dict
    ) -> "DetectionPreview | None":
        """Spots to draw on the raw-data viewer, for a parameter widget's preview.

        A widget with a ``previewCheckbox`` and a ``sigPreviewToggled`` signal
        gets the plugin's preview drawn over the raw frames while the box is
        checked: the localizer's candidate spots on the displayed frame, the
        MoNaLISA lattice reconstructor's foci. ``displayed_image`` is the 2D
        frame on screen, ``params`` the widget's values. ``None`` means the
        plugin has nothing to draw; the widget then shows nothing.
        """
        return None

    def inspect_source(self, data_obj: DataObj) -> "SourceInspection | None":
        """Describe source-dependent choices without materializing pixels."""
        return self.inspect_acquisition(data_obj)

    def inspect_acquisition(self, data_obj: DataObj) -> "SourceInspection | None":
        """Return the generic semantic preflight for an opted-in plugin."""
        requirements = self.acquisition_requirements
        if requirements is None:
            return None
        try:
            resolved = data_obj.acquisition_layout
        except AcquisitionLayoutResolutionError as exc:
            return SourceInspection(
                source_kind=getattr(data_obj, "sourceKind", "image"),
                issues=tuple(exc.issues),
            )
        except AttributeError:
            resolved = None
        if not isinstance(resolved, ResolvedAcquisitionLayout):
            # A source that cannot describe its acquisition is not an error by
            # itself; the plugin still has to decide whether it can proceed on
            # explicit parameters alone. Report it so the choice stays visible
            # instead of raising out of a preflight the plugin opted into.
            return SourceInspection(
                source_kind=getattr(data_obj, "sourceKind", "image"),
                issues=(
                    LayoutIssue(
                        "warning",
                        "NO_RESOLVED_ACQUISITION_LAYOUT",
                        "This source does not expose a resolved acquisition layout",
                        "AcquisitionLayout",
                    ),
                ),
            )
        issues = preflight_acquisition_layout(resolved, requirements)
        metadata = {
            "acquisition_layout_source": resolved.source,
            "acquisition_layout_confidence": resolved.confidence,
            "acquisition_payload_kind": resolved.layout.payload_kind,
        }
        return SourceInspection(
            source_kind=getattr(data_obj, "sourceKind", "image"),
            metadata=metadata,
            issues=issues,
        )

    def validate_source(self, data_obj: DataObj) -> "SourceInspection | None":
        """Raise before pixel processing when opted-in preflight has errors."""
        inspection = self.inspect_acquisition(data_obj)
        if inspection is None:
            return None
        errors = tuple(issue for issue in inspection.issues if issue.severity == "error")
        if errors:
            raise AcquisitionPreflightError(errors[0].message, errors)
        return inspection


@dataclass(frozen=True)
class SourceChoice:
    """One dynamic value exposed by a source inspection."""

    value: Any
    label: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceInspection:
    """Generic metadata-only description consumed by parameter widgets."""

    source_kind: str
    choices: dict[str, tuple[SourceChoice, ...]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None
    issues: tuple[LayoutIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        if not self.issues:
            return
        highest = next(
            (issue for issue in self.issues if issue.severity == "error"),
            self.issues[0],
        )
        object.__setattr__(self, "warning", highest.message)


RECONSTRUCTION_PHASES = (
    "inspect",
    "align",
    "allocate",
    "assemble",
    "finalize",
)


class ReconstructionCancelled(RuntimeError):
    """Cooperative terminal used to discard a partial reconstruction."""


class CancellationToken:
    """Thread-safe cancellation flag shared by controller and worker."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ReconstructionCancelled("Reconstruction cancelled")


@dataclass(frozen=True)
class ReconstructionProgress:
    """One monotonic worker progress update."""

    phase: str
    completed: int
    total: int
    message: str
    fraction: float


@dataclass(frozen=True)
class ResourceEstimate:
    """Preflight output allocation estimate returned without loading pixels."""

    output_shape: tuple[int, ...]
    canvas_bytes: int
    weight_bytes: int
    description: str = "reconstruction"
    suggestions: tuple[str, ...] = ()

    @property
    def required_bytes(self) -> int:
        return int(self.canvas_bytes) + int(self.weight_bytes)


@dataclass
class ReconstructionContext:
    """Worker-owned progress, cancellation and resource contract."""

    progress_callback: Callable[[ReconstructionProgress], None] | None = None
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    memory_budget_bytes: int | None = None
    confirmed_over_budget: bool = False
    _last_fraction: float = field(default=0.0, init=False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancellation_token.cancelled

    def check_cancelled(self) -> None:
        self.cancellation_token.check()

    def report(
        self,
        phase: str,
        completed: int,
        total: int,
        message: str = "",
    ) -> ReconstructionProgress:
        if phase not in RECONSTRUCTION_PHASES:
            raise ValueError(f"Unknown reconstruction phase {phase!r}")
        completed = max(0, int(completed))
        total = max(1, int(total))
        local = min(1.0, completed / total)
        phase_index = RECONSTRUCTION_PHASES.index(phase)
        fraction = (phase_index + local) / len(RECONSTRUCTION_PHASES)
        # A phase may discover a more accurate total after it starts. Never let
        # that refinement make the visible job move backwards.
        fraction = max(self._last_fraction, min(1.0, fraction))
        self._last_fraction = fraction
        update = ReconstructionProgress(
            phase=phase,
            completed=completed,
            total=total,
            message=str(message or ""),
            fraction=fraction,
        )
        if self.progress_callback is not None:
            self.progress_callback(update)
        return update


@dataclass(frozen=True)
class StackInfo:
    """Metadata for one logical live stack."""

    frame_shape: tuple[int, ...]
    dtype: np.dtype
    attrs: dict[str, Any] = field(default_factory=dict)
    frames_per_stack: int | None = None
    expected_frames: int | None = None
    detector_name: str | None = None
    dataset_path: str | None = None
    source_format: str | None = None
    acquisition_layout: ResolvedAcquisitionLayout | None = None


@dataclass(frozen=True)
class Chunk:
    """Contiguous frame range yielded by a live source."""

    data: np.ndarray
    start: int
    end: int


@dataclass(frozen=True)
class StreamPlan:
    """Output shape and display metadata for a streaming session."""

    out_shape: tuple[int, ...]
    axis_labels: list[str]
    view_modes: list[ViewMode]
    dtype: np.dtype = np.dtype(np.float32)
    scale_unit: str = "px"
    axis_scales: list[float] | None = None


@dataclass(frozen=True)
class StreamInit:
    """First frames plus metadata, without forcing chunks through DataObj."""

    name: str
    dataset_name: str
    data: np.ndarray
    attrs: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    stack_info: StackInfo | None = None


class StreamingSession(ABC):
    """Stateful live reconstruction for one stack or recording."""

    @abstractmethod
    def begin(self, init_obj: StreamInit, params: dict) -> StreamPlan:
        """Inspect the first frames, allocate state, and return the output plan."""
        ...

    @abstractmethod
    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        """Process raw frames in the half-open range ``[start:end]``."""
        ...

    @abstractmethod
    def result(self) -> ProcessingResult:
        """Return a snapshot of the current reconstruction."""
        ...

    def live_plane(self) -> "tuple[int, np.ndarray] | None":
        """One timepoint's output slice, for an incremental viewer update.

        Returns ``(timepoint_index, plane)`` where ``plane`` is a **copy** of
        that timepoint's slice of the output, keeping every axis (so the
        timepoint axis has length 1 and the caller can assign it straight into
        an identically-shaped buffer).

        This is the cheap path for live updates: the viewer keeps its own
        accumulating buffer and writes each plane into it, instead of receiving
        a fresh copy of the whole growing volume on every refresh. The copy is
        what keeps the boundary hard -- the session never hands out memory it
        goes on writing to.

        Returning ``None`` (the default) means the session cannot do
        incremental updates, and callers fall back to :meth:`result`.
        """
        return None

    def finish(self) -> ProcessingResult:
        """Finalize processing and return the final result."""
        return self.result()

    def close(self) -> None:
        """Free optional resources such as GPU buffers."""
        return None


class StreamingReconstructor(Reconstructor):
    """Reconstructor that supports sub-stack live updates."""

    supports_streaming: bool = True

    @abstractmethod
    def make_session(self) -> StreamingSession:
        """Create a fresh session for one live stack or recording."""
        ...


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
