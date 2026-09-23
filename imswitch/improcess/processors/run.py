"""The one way a processor is run, shared by the GUI and headless callers.

These helpers used to live in ``ResultProcessorController``. They have no Qt
in them, but living in a controller module meant a headless runner would
either import a GUI module or re-implement the ROI-restriction and provenance
semantics, and a re-implementation drifts. Every path that applies a
processor -- the processor panels, the image toolbar, a workflow runner --
goes through here, so an output carries the same provenance whichever way
it was made.
"""

from __future__ import annotations

from imswitch.improcess.processors.base import normalize_processor_output


def run_multi(processor, inputs, params: dict, logger):
    """One run consuming every input; the arity contract puts them in params."""
    try:
        output = processor.apply(inputs[0], {**params, "results": inputs})
        # inputs[0] is the primary source for a multi-input run — it is what
        # apply() is handed. Merges of several results keep only that lineage.
        return list(
            normalize_processor_output(
                output, inputs[0], processor, params, inputs
            )
        ), []
    except Exception as exc:
        logger.exception(
            "Failed to run processor %s on %d inputs",
            getattr(processor, "id", type(processor).__name__),
            len(inputs),
        )
        return [], [(inputs[0], str(exc))]


def restriction_for(processor, params: dict):
    """The ROI restriction this run was asked for, or None (P-R).

    Read from one well-known parameter key and honoured only by processors
    that declared they accept one, so a processor that ignores ROIs cannot be
    handed a cropped input by a UI that guessed.
    """
    from imswitch.improcess.analysis.roi_restriction import (
        ROI_PARAM,
        ROIRestriction,
    )

    restriction = params.get(ROI_PARAM)
    if not isinstance(restriction, ROIRestriction) or not restriction.active:
        return None
    if not getattr(processor, "accepts_roi", False):
        return None
    return restriction


def run_restricted(processor, input_result, params: dict, restriction):
    """Narrow the input, run, then tell the outputs what they were narrowed to.

    Around the processor rather than inside it: `apply` receives an ordinary
    result and needs to know nothing about ROIs, which is what lets every
    existing processor become ROI-aware by declaring one attribute.
    """
    from imswitch.improcess.analysis.roi_restriction import (
        ROI_PARAM,
        apply_provenance,
        restrict_result,
    )

    narrowed, applied = restrict_result(input_result, restriction)
    # The restriction is consumed here, so it does not travel into `apply`.
    # Leaving it in would hand every ROI-aware processor a parameter it has no
    # use for — and one holding the ROIs themselves, so a processor that keeps
    # its params (segmentation does) would retain their mask payloads and put
    # a non-serialisable object anywhere params are later written out.
    inner = {key: value for key, value in params.items() if key != ROI_PARAM}
    output = processor.apply(narrowed, inner)
    # Provenance is attached against the *original* input, not the narrowed
    # copy: the narrowed one is an implementation detail that never existed
    # outside this call, and lineage pointing at it would dangle.
    #
    # The step records the region alongside the processor's own settings.
    # "Filtered with radius 3" is only half of what happened when it was
    # filtered inside an ROI, and the region is the half that cannot be
    # guessed from the output. It is recorded by the runner, with its
    # geometry, because no processor ever sees it.
    results = list(
        normalize_processor_output(
            output, input_result, processor, inner, restriction=applied
        )
    )
    apply_provenance(results, applied)
    return results


def run_batch(processor, inputs, params: dict, logger):
    """One run per input. A failure is reported, not fatal: one bad result
    part-way through a sweep must not throw away the ones that worked."""
    results = []
    failures = []
    restriction = restriction_for(processor, params)
    for input_result in inputs:
        try:
            if restriction is not None:
                results.extend(
                    run_restricted(processor, input_result, params, restriction)
                )
                continue
            output = processor.apply(input_result, params)
            results.extend(
                normalize_processor_output(output, input_result, processor, params)
            )
        except Exception as exc:
            logger.exception(
                "Failed to run processor %s on %s",
                getattr(processor, "id", type(processor).__name__),
                getattr(input_result, "name", type(input_result).__name__),
            )
            failures.append((input_result, str(exc)))
    return results, failures


def run_processor(processor, inputs, params: dict, logger):
    """Dispatch on the processor's arity: one run over all inputs, or one per input.

    Returns ``(results, failures)`` like the two it wraps.
    """
    inputs = list(inputs) if isinstance(inputs, (list, tuple)) else [inputs]
    if getattr(processor, "max_inputs", 1) != 1:
        return run_multi(processor, inputs, params, logger)
    return run_batch(processor, inputs, params, logger)


def summarize(results, failures) -> str:
    """A one-line status for a run: what was created, and the first failure."""
    created = (
        f"Created {getattr(results[0], 'name', 'result')}."
        if len(results) == 1
        else f"Created {len(results)} results."
    )
    if not failures:
        return created
    name = getattr(failures[0][0], "name", "one input")
    return f"{created} {len(failures)} failed — '{name}': {failures[0][1]}"


__all__ = [
    "restriction_for",
    "run_batch",
    "run_multi",
    "run_processor",
    "run_restricted",
    "summarize",
]


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
