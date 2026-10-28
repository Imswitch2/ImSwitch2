"""Detector bindings decide what is recorded, checked against the wire
(docs/design/plans/scan-engines-and-expert-scan.md §2.5, §7.10).

A camera takes a frame on every rising edge of its line: a binding that
expects other frames, step by step, is refused before anything runs, with
the step and its phase; one that agrees is what the layout and the frame
counts come from. A detector without a binding is recorded as its line
fires. What is not built is refused, not approximated.
"""

import dataclasses

import pytest

from imswitch.imcommon.model.acquisition_layout import recorded_frame_count
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_program_layouts,
)
from imswitch.imcontrol.model.scan_program import (
    DetectorBinding, Loop, ScanProgram, TimelineEvent, presets,
)
from imswitch.imcontrol.model.scan_program.bindings import (
    binding_lines, binding_problems, frames_per_step, wire_frames,
)
from imswitch.imcontrol.model.scan_program.program import Trajectory

from .test_scan_program_expert_model import CHANNELS, MS_RESOLFT, NIDAQ, RASTER


def _ms_resolft(*bindings):
    program = presets.make('ms-resolft', MS_RESOLFT)
    return program if not bindings else dataclasses.replace(program, detectors=bindings)


def _phased_raster(camera_phases, binding):
    """A raster with two phases; the camera exposes in ``camera_phases``."""
    raster = presets.make('raster', dict(RASTER, camera='Camera'))
    return ScanProgram(
        loops=(raster.loops[0], Loop('phase', 'condition', 2), raster.loops[1]),
        drives=raster.drives, step_s=raster.step_s,
        timeline=tuple(e if e.device != 'Camera' else
                       TimelineEvent('Camera', 'detector', e.start_s, e.end_s,
                                     phases=camera_phases, id='exposure')
                       for e in raster.timeline),
        detectors=(binding,), timing_policy=raster.timing_policy)


def _problems(program, **kwargs):
    return binding_problems(program, NIDAQ.compile(program, CHANNELS).schedule, **kwargs)


# --- a camera: its binding against its line --------------------------------------------

def test_a_binding_that_agrees_with_its_line_is_what_the_layout_records():
    program = _ms_resolft(DetectorBinding('Camera', events='exposure'))
    schedule = NIDAQ.compile(program, CHANNELS).schedule
    assert binding_problems(program, schedule) == []
    assert frames_per_step(program, schedule, 'Camera') == wire_frames(schedule, 'Camera') \
        == [1] * 12
    layout = build_program_layouts(program, schedule, ['Camera'], scan_source='x')['Camera']
    assert recorded_frame_count(layout) == 12
    assert binding_lines(program, schedule, ['Camera']) == [
        'Camera: 1 frame per step; its trigger line agrees.']


def test_a_binding_that_expects_frames_its_line_does_not_fire_is_refused_at_the_step():
    program = _ms_resolft(DetectorBinding('Camera', events='exposure', per_step=2))
    # the layout is the binding's -- which is why the panel refuses it first
    schedule = NIDAQ.compile(program, CHANNELS).schedule
    layout = build_program_layouts(program, schedule, ['Camera'], scan_source='x')['Camera']
    assert recorded_frame_count(layout) == 24
    assert _problems(program) == [
        "Camera's binding takes 2 frames per step, but its line fires 1 in step 1 of 12: a "
        'camera takes a frame on every rising edge of its line.']


def test_a_line_that_fires_in_a_phase_the_binding_leaves_out_is_refused():
    leaves_out = _phased_raster(None, DetectorBinding('Camera', events='exposure',
                                                      phases=frozenset({1})))
    assert _problems(leaves_out) == [
        "Camera's binding leaves out phase 0, but its line fires 1 in step 1 of 24 (phase 0): "
        'the camera would take frames the recording does not expect.']
    expects = _phased_raster(frozenset({1}), DetectorBinding('Camera', events='exposure',
                                                             phases=frozenset({0, 1})))
    assert _problems(expects) == [
        "Camera's binding takes 1 frame per step in phases 0, 1, but its line fires 0 in "
        'step 1 of 24 (phase 0): a camera takes a frame on every rising edge of its line.']
    agrees = _phased_raster(frozenset({1}), DetectorBinding('Camera', events='exposure',
                                                            phases=frozenset({1})))
    assert _problems(agrees) == []


def test_a_camera_takes_frames_from_its_own_line_only():
    program = _ms_resolft(DetectorBinding('Camera', events='L405'))
    assert _problems(program) == [
        "Camera's binding: it takes the events 'L405' of L405, but a camera takes frames from "
        'its own line: bind events on Camera.']


def test_a_detector_without_a_binding_is_recorded_as_its_line_fires():
    program = dataclasses.replace(_ms_resolft(), detectors=())
    schedule = NIDAQ.compile(program, CHANNELS).schedule
    assert binding_problems(program, schedule) == []
    assert frames_per_step(program, schedule, 'Camera') == [1] * 12
    assert binding_lines(program, schedule, ['Camera']) == [
        'Camera: not in Recording; recorded as its trigger line fires (1 frame per step).']


# --- what is not built ------------------------------------------------------------------

def test_what_is_not_built_is_refused_with_the_reason():
    program = _ms_resolft(
        DetectorBinding('Camera', events='exposure', window='step', assembly='image'),
        DetectorBinding('APD', events='L488', per_step=2, window='event', assembly='stream'))
    assert _problems(program, point_detectors=['APD']) == [
        "Camera's binding: a camera takes a stream of frames (assembly 'stream'); an image "
        "assembled from windows is a point detector's.",
        "Camera's binding: a camera takes a frame per pulse on its line (window 'event'); a "
        "window of a whole step is a point detector's.",
        "APD's binding: a point detector assembles an image (assembly 'image'); a stream of "
        'its values is not built.',
        "APD's binding: a point detector integrates every sample of each step (window "
        "'step'); integrating only during an event is not built.",
        "APD's binding: a point detector takes one value per step, in every phase; other "
        'counts or phases are not built.']
    built = _ms_resolft(DetectorBinding('APD', events='L488', window='step', assembly='image'))
    assert _problems(built, point_detectors=['APD']) == []


def test_two_bindings_an_unknown_detector_and_a_trajectory_say_so():
    twice = _ms_resolft(DetectorBinding('Camera', events='exposure'),
                        DetectorBinding('Camera', events='exposure'))
    assert _problems(twice) == ['Camera has two bindings; one says what it takes.']
    assert _problems(_ms_resolft(DetectorBinding('Camera', events='exposure')),
                     known_detectors=['APD']) == [
        "Camera's binding: this setup has no detector Camera."]
    program = ScanProgram(loops=(), drives=(), step_s=1e-4, trajectory=Trajectory(
        ('X', 'Y'), 'circle', {'center_um': [0, 0], 'radius_um': 1.0, 'turns': 1}, 0.01, 0.0,
        0.5), detectors=(DetectorBinding('APD', events='X'),))
    assert binding_problems(program, None) == [
        "APD's binding: on a trajectory the point detectors measure window by window and "
        'cameras take no frames, so a binding is not read; remove it.']


# --- the panel ------------------------------------------------------------------------

def test_the_panel_refuses_a_run_whose_binding_disagrees_and_reports_what_is_recorded(qtbot):
    from .test_scan_controller_expert import ExpertRig, _ms_resolft as made
    rig = ExpertRig()
    try:
        program = made(rig, timepoints=1)
        rig.scan.setProgram(program)
        report = rig.widget.reportText()
        assert 'Camera: 1 frame per step; its trigger line agrees.' in report
        assert rig.scan.getNumCamTTL() == {'Camera': 1}

        rig.scan.setProgram(dataclasses.replace(program, detectors=(
            DetectorBinding('Camera', events='exposure', per_step=2),)))
        report = rig.widget.reportText()
        assert report.startswith('Not runnable on nidaq.')
        assert ("The detector bindings do not hold:\n- Camera's binding takes 2 frames per "
                'step, but its line fires 1 in step 1 of 6') in report
        assert rig.scan.getNumCamTTL() == {}
        rig.scan.runScanAdvanced()
        assert rig.rejected and 'The detector bindings do not hold' in rig.rejected[-1]
        assert not rig.scan.isRunning
    finally:
        rig.close()


def test_a_camera_on_a_follower_is_checked_against_the_followers_line(qtbot):
    from .test_scan_controller_expert import ExpertRig, _ms_resolft as made, _setup_dict
    setup = _setup_dict(engine='lead')
    setup['scanEngines'] = {
        'lead': {'managerName': 'SimulatedStepEngine', 'managerProperties': {
            'timingGridS': 1e-5, 'digitalLines': ['lead/ttl0', 'lead/ttl3'],
            'clockAccuracyPpm': 20.0}},
        'follow': {'managerName': 'StepProtocolScanEngine', 'triggerInput': 'lead/ttl3',
                   'managerProperties': {'port': 'simulated', 'simulatedBoard': {
                       'gridNs': 10_000, 'dacs': {'0': [0, 10]}, 'ttlLines': 2,
                       'triggerIn': True, 'triggerLatencyTicks': 2, 'triggerMinTicks': 1,
                       'clockPpm': 20.0}}}}
    setup['positioners']['Z']['analogChannel'] = 'follow/dac0'
    first, second = list(setup['lasers'])[:2]
    setup['lasers'][first]['digitalLine'] = 'lead/ttl0'
    setup['lasers'][second]['digitalLine'] = 'follow/ttl0'
    setup['detectors']['Camera']['digitalLine'] = 'follow/ttl1'
    rig = ExpertRig(setup)
    try:
        program = made(rig, timepoints=1)
        rig.scan.setProgram(program)
        report = rig.widget.reportText()
        assert report.startswith('Runnable on lead.'), report
        assert 'Camera: 1 frame per step; its trigger line agrees.' in report
        rig.scan.setProgram(dataclasses.replace(program, detectors=(
            DetectorBinding('Camera', events='exposure', per_step=2),)))
        assert 'but its line fires 1 in step 1 of 6' in rig.widget.reportText()
    finally:
        rig.close()


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
