"""Polarisation map: which control settings reach which polarisation states.

Input is a measurement run (``imcommon.model.measurement_run``) in which a
polarimeter was sampled at a sequence of control settings, typically two
waveplate angles. For every committed point, the paired raw samples are
aggregated as Stokes vectors (``imcommon.algorithms.polarisation``). Each
target state (right/left circular, linear every N°) is then matched to the
nearest **eligible** point on the Poincaré sphere.

Statuses: ``pass``, ``failed`` (no eligible point within the threshold), or
``unqualified`` (the point was acquired with unverified timing — never a
pass). The result is a table, one row per target, shown with napari layers:
the sphere, the measured points, one path per grid line, and the targets.
Saving it as JSON writes the export a replay script reads
(``docs/design/plans/transient-instruments-step-scans.md`` §9.6).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.polarisation import (
    PASS,
    aggregate_polarisation,
    default_targets,
    match_targets,
)
from imswitch.imcommon.model.measurement_run import MeasurementRunFile
from imswitch.improcess.model.dataset_sources import MEASUREMENT_RUN_SOURCE_KIND
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.points_table_result import PointsTableResult
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.reconstructors.base import ReconstructionContext, Reconstructor

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


CONVENTION_RCP_POSITIVE = 'rcp = +s3'
CONVENTION_RCP_NEGATIVE = 'rcp = -s3'

#: Export refused when the instrument's wavelength setting and the declared
#: illumination differ by more than this.
WAVELENGTH_TOLERANCE_NM = 2.0

_STATUS_COLOURS = {
    'pass': [0.15, 0.75, 0.3, 1.0],
    'failed': [0.9, 0.2, 0.2, 1.0],
    'unqualified': [1.0, 0.65, 0.0, 1.0],
}


class PolarisationMapError(ValueError):
    """The source cannot be analysed as a polarisation map."""


class _PolarisationParamsWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        form = QtWidgets.QFormLayout(self)
        defaults = PolarisationMapReconstructor.default_params()

        self.instrument = QtWidgets.QLineEdit(self)
        self.instrument.setPlaceholderText('the only polarimeter in the run')
        self.instrument.setToolTip(
            'Which polarimeter to analyse when the run sampled more than one. '
            'Instruments are never combined.')
        form.addRow('Instrument', self.instrument)

        self.threshold = QtWidgets.QDoubleSpinBox(self)
        self.threshold.setRange(0.1, 90.0)
        self.threshold.setDecimals(1)
        self.threshold.setSuffix(' °')
        self.threshold.setValue(defaults['threshold_deg'])
        self.threshold.setToolTip(
            'A target passes when an eligible measured point lies within this '
            'angle of it on the Poincaré sphere.')
        form.addRow('Match within', self.threshold)

        self.dop_min = QtWidgets.QDoubleSpinBox(self)
        self.dop_min.setRange(0.0, 1.0)
        self.dop_min.setDecimals(3)
        self.dop_min.setSingleStep(0.01)
        self.dop_min.setValue(defaults['dop_min'])
        self.dop_min.setToolTip(
            'Points are eligible only if both the instrument-reported DOP and '
            'the DOP of the averaged Stokes vector reach this value.')
        form.addRow('Minimum DOP', self.dop_min)

        self.linear_step = QtWidgets.QDoubleSpinBox(self)
        self.linear_step.setRange(1.0, 90.0)
        self.linear_step.setDecimals(1)
        self.linear_step.setSuffix(' °')
        self.linear_step.setValue(defaults['linear_step_deg'])
        self.linear_step.setToolTip('Linear target states every this many degrees.')
        form.addRow('Linear targets every', self.linear_step)

        self.convention = QtWidgets.QComboBox(self)
        self.convention.addItems([CONVENTION_RCP_POSITIVE, CONVENTION_RCP_NEGATIVE])
        self.convention.setCurrentText(defaults['convention'])
        self.convention.setToolTip(
            'Which sign of s3 is right circular. Verify it on the rig with a '
            'known quarter-wave plate before trusting RCP/LCP results.')
        form.addRow('Handedness', self.convention)

    def get_values(self) -> dict:
        text = self.instrument.text().strip()
        return {
            'instrument': text or None,
            'threshold_deg': float(self.threshold.value()),
            'dop_min': float(self.dop_min.value()),
            'linear_step_deg': float(self.linear_step.value()),
            'convention': self.convention.currentText(),
        }


class PolarisationMapReconstructor(Reconstructor):
    name = 'Polarisation map'
    id = 'polarisation-map'
    file_extensions = ['h5', 'hdf5']
    description = ('Poincaré-sphere coverage of a waveplate scan, and the settings '
                   'that reach circular and linear states')
    accepted_source_kinds = (MEASUREMENT_RUN_SOURCE_KIND,)
    default_save_subdir = 'polarisation'

    @classmethod
    def default_params(cls) -> dict:
        return {
            'instrument': None,
            'threshold_deg': 5.0,
            'dop_min': 0.95,
            'linear_step_deg': 10.0,
            'convention': CONVENTION_RCP_POSITIVE,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField('instrument', 'text', None, label='Instrument', nullable=True,
                       help='Which polarimeter to analyse when the run sampled more '
                            'than one; empty takes the only one.'),
            ParamField('threshold_deg', 'float', 5.0, label='Match within',
                       min=0.1, max=90.0, decimals=1, suffix='°',
                       help='Angle on the Poincaré sphere within which a target passes.'),
            ParamField('dop_min', 'float', 0.95, label='Minimum DOP', min=0.0, max=1.0,
                       decimals=3, step=0.01,
                       help='Both DOP definitions must reach this for a point to be eligible.'),
            ParamField('linear_step_deg', 'float', 10.0, label='Linear targets every',
                       min=1.0, max=90.0, decimals=1, suffix='°'),
            ParamField('convention', 'select', CONVENTION_RCP_POSITIVE, label='Handedness',
                       options=(CONVENTION_RCP_POSITIVE, CONVENTION_RCP_NEGATIVE),
                       help='Which sign of s3 is right circular (verify on the rig).'),
        )

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return _PolarisationParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget):
        return None

    def process(self, data_obj: 'DataObj', params: dict,
                context: Optional[ReconstructionContext] = None) -> 'PolarisationMapResult':
        run = _run_from(data_obj)
        p = dict(self.default_params())
        p.update(params or {})
        return analyse_run(run, p)


# --------------------------------------------------------------------- analysis
def _run_from(data_obj) -> MeasurementRunFile:
    source = getattr(data_obj, 'sourceMetadata', None)
    if isinstance(source, MeasurementRunFile):
        return source
    path = getattr(data_obj, 'dataPath', None) or source
    if not path:
        raise PolarisationMapError('The polarisation map needs a measurement run file.')
    return MeasurementRunFile.load(path)


def analyse_run(run: MeasurementRunFile, params: Dict[str, Any]) -> 'PolarisationMapResult':
    instrument = _choose_instrument(run, params.get('instrument'))
    inst = run.instruments[instrument]
    for quantity in ('polarisation.azimuth', 'polarisation.ellipticity', 'polarisation.dop'):
        if inst.quantity(quantity) is None:
            raise PolarisationMapError(
                f'Instrument {instrument!r} reports no {quantity}; a polarisation map '
                f'needs azimuth, ellipticity and DOP (DOP decides eligibility).')
    controls = run.control_names()
    if not controls:
        raise PolarisationMapError('The run set no controls; nothing to map.')

    committed = run.committed_mask()
    n = run.n_points
    directions = np.full((n, 3), np.nan)
    defined = np.zeros(n, bool)
    dop_inst = np.full(n, np.nan)
    dop_agg = np.full(n, np.nan)
    dispersion = np.full(n, np.nan)
    power = np.full(n, np.nan)
    n_samples = np.zeros(n, int)
    verified = np.zeros(n, bool)
    for row in range(n):
        if not committed[row]:
            continue
        block = run.point_samples(instrument, row)
        if len(block) == 0:
            continue
        power_col = inst.column(block, 'optical.power')
        agg = aggregate_polarisation(
            inst.column(block, 'polarisation.azimuth'),
            inst.column(block, 'polarisation.ellipticity'),
            inst.column(block, 'polarisation.dop'),
            power_col,
        )
        n_samples[row] = agg.n
        defined[row] = agg.defined
        directions[row] = agg.direction
        dop_inst[row] = agg.dop_instrument
        dop_agg[row] = agg.dop_aggregate
        dispersion[row] = agg.dispersion_deg
        power[row] = agg.power_mean
        verified[row] = (run.window_verified(instrument, row)
                         and bool(np.all(block['verified'] == 1)))

    dop_min = float(params['dop_min'])
    with np.errstate(invalid='ignore'):
        eligible = committed & defined & (dop_inst >= dop_min) & (dop_agg >= dop_min)
    rcp_sign = 1.0 if params['convention'] == CONVENTION_RCP_POSITIVE else -1.0
    targets = default_targets(float(params['linear_step_deg']), rcp_sign=rcp_sign)
    matches = match_targets(targets, directions, eligible, verified,
                            float(params['threshold_deg']))

    values = {name: run.control_values(name) for name in controls}
    rows_dirs, props = [], {'target': [], 'status': []}
    for name in controls:
        props[f'{name} ({run.control_unit(name)})'] = []
    props.update({'distance (°)': [], 'DOP (instrument)': [], 'DOP (aggregate)': [],
                  'power': [], 'dispersion (°)': [], 'angles': [], 'point': []})
    for target, match in zip(targets, matches):
        rows_dirs.append(target.direction)
        props['target'].append(match.target)
        props['status'].append(match.status)
        idx = match.index
        for name in controls:
            props[f'{name} ({run.control_unit(name)})'].append(
                float(values[name][0][idx]) if idx is not None else math.nan)
        props['distance (°)'].append(match.distance_deg)
        props['DOP (instrument)'].append(float(dop_inst[idx]) if idx is not None else math.nan)
        props['DOP (aggregate)'].append(float(dop_agg[idx]) if idx is not None else math.nan)
        props['power'].append(float(power[idx]) if idx is not None else math.nan)
        props['dispersion (°)'].append(float(dispersion[idx]) if idx is not None else math.nan)
        props['angles'].append(
            'measured' if idx is not None and all(values[c][1][idx] for c in controls)
            else ('commanded' if idx is not None else ''))
        props['point'].append(int(run.points['point'][idx]) if idx is not None else -1)

    summary = {
        'run_id': run.run_id,
        'source': str(run.path),
        'instrument': instrument,
        'points': n,
        'committed': int(committed.sum()),
        'eligible': int(eligible.sum()),
        'unverified_points': int((committed & ~verified).sum()),
        'pass': sum(m.status == PASS for m in matches),
        'targets': len(matches),
    }
    result = PolarisationMapResult(
        'Polarisation map', np.array(rows_dirs),
        axis_names=['s1', 's2', 's3'], properties=props, scale_unit='',
        metadata={'summary': summary, 'params': dict(params)},
    )
    result.run = run
    result.instrument = instrument
    result.params = dict(params)
    result.point_directions = directions
    result.point_eligible = eligible
    result.point_committed = committed
    result.controls = controls
    return result


def _choose_instrument(run: MeasurementRunFile, requested: Optional[str]) -> str:
    candidates = run.instruments_with_quantity('polarisation.azimuth')
    if requested:
        if requested not in run.instruments:
            raise PolarisationMapError(
                f'Instrument {requested!r} is not in this run (it has {sorted(run.instruments)}).')
        return requested
    if not candidates:
        raise PolarisationMapError('This run sampled no polarimeter.')
    if len(candidates) > 1:
        raise PolarisationMapError(
            f'This run sampled several polarimeters ({", ".join(candidates)}); choose one '
            f'under "Instrument". Instruments are never combined.')
    return candidates[0]


# ----------------------------------------------------------------------- result
class PolarisationMapResult(PointsTableResult):
    """Target table + Poincaré layers; saves as CSV, HDF5 or the JSON export."""

    supported_formats = ('json', 'csv', 'hdf5')

    run: MeasurementRunFile
    instrument: str
    params: Dict[str, Any]

    # ------------------------------------------------------------ display
    def display_layers(self) -> List[DisplayLayerSpec]:
        labels = ['s1', 's2', 's3']
        meta3d = {'ndisplay': 3}
        layers = [
            DisplayLayerSpec('Poincaré sphere', _sphere_wireframe(), labels,
                             kind='shapes', role='context', component='sphere',
                             metadata=dict(meta3d),
                             layer_kwargs={'shape_type': 'path', 'edge_width': 0.004,
                                           'edge_color': [0.6, 0.6, 0.6, 0.6]}),
            DisplayLayerSpec('S1 / S2 / S3 axes', _axes_lines(), labels,
                             kind='shapes', role='context', component='axes',
                             metadata=dict(meta3d),
                             layer_kwargs={'shape_type': 'path', 'edge_width': 0.008,
                                           'edge_color': [[0.9, 0.3, 0.3, 1], [0.3, 0.8, 0.3, 1],
                                                          [0.3, 0.5, 0.95, 1]]}),
        ]
        dirs = self.point_directions
        keep = self.point_committed & np.all(np.isfinite(dirs), axis=1)
        if keep.any():
            colours = self._point_colours()[keep]
            colours[~self.point_eligible[keep], 3] = 0.35
            layers.append(DisplayLayerSpec(
                'Measured states', dirs[keep], labels, kind='points', role='primary',
                component='measured', metadata=dict(meta3d),
                layer_kwargs={'size': 0.03, 'face_color': colours, 'border_width': 0}))
        lines = self._grid_lines()
        if lines is not None:
            paths, colours = lines
            layers.append(DisplayLayerSpec(
                'Scan lines', paths, labels, kind='shapes', role='overlay',
                component='lines', metadata=dict(meta3d),
                layer_kwargs={'shape_type': 'path', 'edge_width': 0.006,
                              'edge_color': colours}))
        status = list(self.properties['status'])
        layers.append(DisplayLayerSpec(
            'Targets', np.asarray(self.coordinates), labels, kind='points', role='overlay',
            component='targets', metadata=dict(meta3d),
            layer_kwargs={'size': 0.07, 'symbol': 'ring',
                          'face_color': [_STATUS_COLOURS.get(s, [1, 1, 1, 1]) for s in status]}))
        return layers

    def _point_colours(self) -> np.ndarray:
        n = len(self.point_directions)
        grid = self.run.grid
        if grid is not None and len(grid.axes) >= 2:
            line = self.run.points['grid_index'][:, 0].astype(float)
            span = max(1.0, float(grid.shape[0] - 1))
            fraction = line / span
        else:
            fraction = np.arange(n) / max(1, n - 1)
        return _hsv(fraction * 0.85)

    def _grid_lines(self):
        """One path per outer-axis value, through the inner axis in order."""
        grid = self.run.grid
        if grid is None or len(grid.axes) != 2:
            return None
        shape = grid.shape
        index = self.run.points['grid_index']
        dirs = self.point_directions
        table = np.full(shape + (3,), np.nan)
        for row in range(len(dirs)):
            if self.point_committed[row]:
                table[int(index[row, 0]), int(index[row, 1])] = dirs[row]
        paths, colours = [], []
        palette = _hsv(np.arange(shape[0]) / max(1, shape[0] - 1) * 0.85)
        for line in range(shape[0]):
            path = table[line]
            if np.isfinite(path).all() and len(path) >= 2:
                paths.append(path)
                colours.append(palette[line])
        if not paths:
            return None
        return np.stack(paths), np.array(colours)

    # ---------------------------------------------------------------- saving
    def plan_save(self, path: Path, fmt: str):
        from imswitch.improcess.model.save_protocol import SavePlan

        if fmt == 'json':
            return SavePlan(Path(path), fmt)
        return super().plan_save(path, fmt)

    def write_files(self, plan, document) -> None:
        if plan.fmt == 'json':
            export = self.export_document()
            Path(plan.primary).write_text(json.dumps(export, indent=2), encoding='utf-8')
            return
        super().write_files(plan, document)

    def export_document(self) -> Dict[str, Any]:
        """The replay export (§9.6). Refuses when the wavelength is not established."""
        run = self.run
        meta = run.metadata
        inst_meta = (meta.get('instruments') or {}).get(self.instrument, {})
        setting_nm = (inst_meta.get('settings') or {}).get('wavelength_nm')
        illumination = meta.get('illumination') or {}
        source_nm = illumination.get('wavelength_nm')
        if source_nm is None:
            raise PolarisationMapError(
                'Export refused: the run declared no illumination source, so the '
                'wavelength the table holds for is unknown.')
        if setting_nm is not None and abs(float(setting_nm) - float(source_nm)) > WAVELENGTH_TOLERANCE_NM:
            raise PolarisationMapError(
                f'Export refused: the instrument was set to {setting_nm} nm but the '
                f'declared illumination is {source_nm} nm.')
        controls = []
        for entry in meta.get('controls', ()):
            controls.append({
                'name': entry['name'], 'unit': entry.get('unit', ''),
                'resource': entry.get('resource'),
                'zero_reference': entry.get('zero_reference', {'state': 'unknown'}),
            })
        targets = []
        props = self.properties
        for i, name in enumerate(props['target']):
            angles = {c: _finite_or_none(props[f'{c} ({run.control_unit(c)})'][i])
                      for c in self.controls}
            targets.append({
                'target': str(name),
                'status': str(props['status'][i]),
                'settings': angles,
                'angles_are': str(props['angles'][i]) or None,
                'distance_deg': _finite_or_none(props['distance (°)'][i]),
                'dop_instrument': _finite_or_none(props['DOP (instrument)'][i]),
                'dop_aggregate': _finite_or_none(props['DOP (aggregate)'][i]),
            })
        return {
            'format': 'imswitch-polarisation-states',
            'version': 1,
            'source_file': str(run.path),
            'run_id': run.run_id,
            'instrument': {'name': self.instrument,
                           'identity': inst_meta.get('identity', {}),
                           'wavelength_setting_nm': setting_nm},
            'illumination': illumination,
            'plane_label': meta.get('plane_label', ''),
            'convention': self.params.get('convention'),
            'threshold_deg': self.params.get('threshold_deg'),
            'dop_min': self.params.get('dop_min'),
            'controls': controls,
            'created': meta.get('created'),
            'targets': targets,
        }


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hsv(fraction) -> np.ndarray:
    h = np.asarray(fraction, dtype=float) % 1.0
    i = np.floor(h * 6).astype(int) % 6
    f = h * 6 - np.floor(h * 6)
    q, t = 1 - f, f
    rgb = np.select(
        [i[:, None] == k for k in range(6)],
        [np.stack(c, axis=-1) for c in [
            (np.ones_like(h), t, np.zeros_like(h)), (q, np.ones_like(h), np.zeros_like(h)),
            (np.zeros_like(h), np.ones_like(h), t), (np.zeros_like(h), q, np.ones_like(h)),
            (t, np.zeros_like(h), np.ones_like(h)), (np.ones_like(h), np.zeros_like(h), q)]],
    )
    return np.column_stack([rgb, np.ones(len(h))])


_PATH_VERTICES = 73


def _sphere_wireframe() -> np.ndarray:
    """Meridians and parallels every 30°, all with the same vertex count."""
    t = np.linspace(0, 2 * np.pi, _PATH_VERTICES)
    paths = []
    for lon in np.radians(np.arange(0, 180, 30)):
        paths.append(np.column_stack([np.cos(t) * np.cos(lon), np.cos(t) * np.sin(lon), np.sin(t)]))
    for lat in np.radians([-60, -30, 0, 30, 60]):
        paths.append(np.column_stack([np.cos(lat) * np.cos(t), np.cos(lat) * np.sin(t),
                                      np.full_like(t, np.sin(lat))]))
    return np.stack(paths)


def _axes_lines() -> np.ndarray:
    s = np.linspace(-1.25, 1.25, _PATH_VERTICES)
    z = np.zeros_like(s)
    return np.stack([np.column_stack([s, z, z]), np.column_stack([z, s, z]),
                     np.column_stack([z, z, s])])
