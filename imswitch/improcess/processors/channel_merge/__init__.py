"""Channel-merge processor."""

from .processor import (
    CHANNEL_AXIS_LABEL,
    ChannelMergeProcessor,
    can_merge_results,
    merge_compatibility,
    merge_results,
)

__all__ = [
    "CHANNEL_AXIS_LABEL",
    "ChannelMergeProcessor",
    "can_merge_results",
    "merge_compatibility",
    "merge_results",
]
