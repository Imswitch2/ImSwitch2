"""Feedback-specific parameter definitions and compatibility aliases."""

from __future__ import annotations

from types import MappingProxyType

from ..measurement_metrics import INTENSITY_ANALYSIS_PARAMS

# Compatibility alias retained for existing callers. The parameter itself is
# measurement/intensity-analysis owned rather than feedback-algorithm owned.
INTENSITY_FEEDBACK_PARAMS = INTENSITY_ANALYSIS_PARAMS


# The first position-correction implementation needs no numerical tuning beyond
# the shared localization/registration stage.
POSITION_CORRECTION_PARAMS = MappingProxyType({})
