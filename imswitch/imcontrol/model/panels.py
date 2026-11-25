"""Which ImControl panels exist, and which can be added while ImSwitch runs.

One entry per panel key -- the key a setup file lists in
``availableWidgets``. The dock tables in ``view/ImConMainView.py``, the lazy
widget and controller tables and the Config Studio's widget catalog each list
panels for their own purpose; this registry does not repeat what they say
(classes, dock titles and positions are looked up there) but adds what none
of them knows: whether a panel can be built while ImSwitch runs, and if not,
why. ``test_panel_registry.py`` fails when a panel appears in one of those
tables and not here, or when a group or display name here disagrees.

Kinds:

``CORE``
    Built at startup from the setup file and assumed by the rest of ImControl
    (shortcuts, recording, the viewer). Not offered at run time.
``LOADABLE``
    Can be added as it is: building it touches no hardware and needs nothing
    that only exists when the setup file listed it.
``CONFIGURED``
    Can be added once the setup file section it reads is configured; adding
    it asks for that section (the Config Studio's form for it) when it is
    missing.
``STARTUP``
    Only built at startup; ``reason`` says why, in words the Add panel dialog
    shows.

Adding a panel the registry does not know yet (for example ``Timelapse`` or
``ScriptRunner`` once they land) takes one ``PanelSpec`` entry here, beside
its lazy widget/controller and dock-table entries.

Qt-free on purpose: the registry is read by the view, the controller and the
tests alike.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, Optional, Tuple


class PanelKind(str, Enum):
    CORE = 'core'
    LOADABLE = 'loadable'
    CONFIGURED = 'configured'
    STARTUP = 'startup'


@dataclass(frozen=True)
class PanelSpec:
    key: str
    """ The ``availableWidgets`` key. """

    displayName: str
    """ The dock title (``_DOCK_DISPLAY_NAMES`` / the dock tables). """

    group: str
    """ The Config Studio's widget group (``Core``, ``Control``, ...). """

    kind: PanelKind

    reason: str = ''
    """ Why a CORE or STARTUP panel cannot be added at run time. """

    requiresPanels: Tuple[str, ...] = ()
    """ Panels that must already be docked (the viewer, for overlays). """

    wiredPeers: Tuple[str, ...] = ()
    """ Panels handed to this one when they exist; build them first. """

    section: Optional[str] = None
    """ The setup-file section this panel reads (``tiling``, ...). """

    silentWithoutSection: bool = False
    """ Its controller does nothing at all without ``section``; ImControl
    shows a message in its dock instead of an empty panel. """

    @property
    def runtimeLoadable(self) -> bool:
        return self.kind in (PanelKind.LOADABLE, PanelKind.CONFIGURED)


_CORE = 'Core panel: built at startup from the setup file.'
_HARDWARE = ('Hardware panel: its devices are set up when ImSwitch starts. '
             'Add it to the setup file and restart.')
_SCAN = 'Decided at startup by the scan type in the setup file. Restart required.'
_UNCHECKED = ('Not yet checked for adding while ImSwitch runs. '
              'Add it to the setup file and restart.')


def _spec(key, displayName, group, kind, reason='', **kwargs):
    return PanelSpec(key=key, displayName=displayName, group=group, kind=kind,
                     reason=reason, **kwargs)


_C, _L, _F, _S = PanelKind.CORE, PanelKind.LOADABLE, PanelKind.CONFIGURED, PanelKind.STARTUP

PANELS: Dict[str, PanelSpec] = {spec.key: spec for spec in (
    # -- Core ---------------------------------------------------------------
    _spec('Image', 'Image Display', 'Core', _C, _CORE),
    _spec('Settings', 'Detector Settings', 'Core', _C, _CORE),
    _spec('View', 'Image Controls', 'Core', _C, _CORE),
    _spec('Recording', 'Recording', 'Core', _C, _CORE),
    _spec('Log', 'Log', 'Core', _C, 'Always there: shown and hidden with View > Log.'),
    _spec('Console', 'Console', 'Core', _L),
    _spec('LineProfile', 'Line Profile', 'Core', _L),
    # Its drawing buttons select the Line Profile panel's modes when there is
    # one, so that one is built first.
    _spec('ViewerTools', 'Viewer Tools', 'Core', _L,
          requiresPanels=('Image',), wiredPeers=('LineProfile',)),
    # -- Analysis -------------------------------------------------------------
    # The alignment tools and uLenses draw overlays on the viewer.
    _spec('AlignAverage', 'Axial Alignment Tool', 'Analysis', _L, requiresPanels=('Image',)),
    _spec('AlignXY', 'Rotational Alignment Tool', 'Analysis', _L, requiresPanels=('Image',)),
    _spec('AlignmentLine', 'Alignment Tool', 'Analysis', _L, requiresPanels=('Image',)),
    _spec('ULenses', 'uLenses Tool', 'Analysis', _L, requiresPanels=('Image',)),
    _spec('FFT', 'FFT Tool', 'Analysis', _L),
    _spec('FLIMHist', 'FLIM Lifetime Histogram', 'Analysis', _L),
    _spec('BeadRec', 'Bead Rec', 'Analysis', _S, _UNCHECKED),
    _spec('BFTimelapse', 'BFTimelapse', 'Analysis', _S, _HARDWARE),
    # -- Advanced -------------------------------------------------------------
    _spec('Watcher', 'File Watcher', 'Advanced', _L),
    _spec('Tiling', 'Tiling', 'Advanced', _F,
          section='tiling', silentWithoutSection=True),
    _spec('SLMs', 'SLMs', 'Advanced', _S, _HARDWARE),
    _spec('SLM', 'SLM', 'Advanced', _S, _HARDWARE),
    _spec('EtSTED', 'EtSTED', 'Advanced', _S, _HARDWARE, section='etSTED'),
    _spec('EtMonalisa', 'EtMonalisa', 'Advanced', _S, _HARDWARE),
    # -- Microscope -----------------------------------------------------------
    _spec('Autofocus', 'Autofocus', 'Microscope', _F,
          section='autofocus', silentWithoutSection=True),
    _spec('FocusLock', 'Focus Lock', 'Microscope', _S, _HARDWARE,
          section='focusLock', silentWithoutSection=True),
    _spec('LeicaStand', 'Stand', 'Microscope', _S, _HARDWARE),
    # -- Control --------------------------------------------------------------
    _spec('Laser', 'Laser Control', 'Control', _S, _HARDWARE),
    _spec('Positioner', 'Positioner', 'Control', _S, _HARDWARE),
    _spec('Rotator', 'Rotator', 'Control', _S, _HARDWARE),
    _spec('RotationScan', 'RotationScan', 'Control', _S, _HARDWARE),
    _spec('MotCorr', 'Motorized Correction Collar', 'Control', _S, _HARDWARE,
          section='microscopeStand'),
    _spec('Scan', 'Scan', 'Control', _S, _SCAN, section='scan'),
    # -- Other ----------------------------------------------------------------
    _spec('SetupModes', 'Setup Modes', 'Other', _L),
    _spec('BSC203', 'BSC203 Stage', 'Other', _S, _HARDWARE),
    _spec('FlipMirror', 'Flip Mirrors', 'Other', _S, _HARDWARE),
    _spec('Instruments', 'Instruments', 'Other', _S,
          'Shown whenever the setup file has instruments.'),
    _spec('LightSheetMulticolor', 'Light-Sheet Multicolor', 'Other', _S, _HARDWARE),
    _spec('EtSnouty', 'EtSnouty', 'Other', _S, _HARDWARE),
    _spec('SetupStatus', 'Setup Status', 'Other', _S,
          'Opens its flip mirrors on serial ports when it is built. '
          'Add it to the setup file and restart.'),
    _spec('WellPlate', 'Well Plate', 'Other', _S,
          'Moves every stage to 0 when it closes. '
          'Add it to the setup file and restart.'),
    _spec('TriggerScopeRaster', 'TriggerScope Raster Scan', 'Other', _S, _SCAN),
    _spec('TriggerScopePLSR', 'TriggerScope pLS-RESOLFT', 'Other', _S, _SCAN),
    _spec('TriggerScopeGalvoDetection', 'TriggerScope Galvo Detection', 'Other', _S, _SCAN),
    _spec('TriggerScopePLSRMulticolor', 'TriggerScope pLS-RESOLFT Multicolor', 'Other', _S, _SCAN),
    _spec('TriggerScopeScan', 'TriggerScope Scan', 'Other', _S, _SCAN),
    _spec('TriggerScopeLSXYR', 'TriggerScope LS-XY-RESOLFT', 'Other', _S, _SCAN),
)}

#: Panels whose failure to build at startup stops ImControl, as it always
#: did: the rest of the application assumes them. Any other panel that fails
#: is reported and left out.
FATAL_AT_STARTUP = frozenset(key for key, spec in PANELS.items() if spec.kind is PanelKind.CORE)

#: The order the Add panel dialog lists groups in (the Config Studio's).
GROUP_ORDER = ('Core', 'Control', 'Microscope', 'Analysis', 'Advanced', 'Other')


def panelSpec(key: str) -> Optional[PanelSpec]:
    return PANELS.get(key)


def isFatalAtStartup(key: str) -> bool:
    """Whether a panel that fails to build at startup must stop ImControl.
    Unknown keys are not: they are as optional as a panel can be."""
    return key in FATAL_AT_STARTUP


@dataclass(frozen=True)
class Availability:
    """What the Add panel dialog may do with one panel right now."""

    action: Optional[str]
    """ ``'show'`` (already docked), ``'add'``, ``'configure'`` (add after
    asking for its section) or ``None`` (not possible; see ``reason``). """

    reason: str = ''

    @property
    def possible(self) -> bool:
        return self.action is not None


def availability(spec: PanelSpec, *, docked: Iterable[str], setupInfo,
                 prerequisiteProblem: Callable[[PanelSpec], Optional[str]] = None
                 ) -> Availability:
    """Whether ``spec`` can be shown or added now, and if not, why.

    ``docked`` is the set of panel keys with a dock (a panel waiting for its
    configuration has one too). ``prerequisiteProblem(spec)`` reports what
    the running hardware lacks for a CONFIGURED panel (an XY stage, ...).
    """
    docked = set(docked)
    if spec.key in docked:
        return Availability('show', 'Already open: shows it.')
    if not spec.runtimeLoadable:
        return Availability(None, spec.reason)
    missing = [peer for peer in spec.requiresPanels if peer not in docked]
    if missing:
        names = ', '.join(PANELS[peer].displayName if peer in PANELS else peer
                          for peer in missing)
        return Availability(None, f'Needs the {names} panel, which this setup does not have.')
    if spec.kind is PanelKind.CONFIGURED:
        problem = prerequisiteProblem(spec) if prerequisiteProblem is not None else None
        if problem:
            return Availability(None, problem)
        if getattr(setupInfo, spec.section, None) is None:
            return Availability(
                'configure', f'Needs a {spec.section} configuration: asks for it first.')
    return Availability('add', '')


def buildOrder(keys: Iterable[str]) -> list:
    """``keys`` in an order that builds each panel's wired peers before it
    (Line Profile before Viewer Tools), otherwise as given."""
    keys = list(keys)
    ordered = []
    for key in keys:
        spec = PANELS.get(key)
        for peer in (spec.wiredPeers if spec else ()):
            if peer in keys and peer not in ordered:
                ordered.append(peer)
        if key not in ordered:
            ordered.append(key)
    return ordered
