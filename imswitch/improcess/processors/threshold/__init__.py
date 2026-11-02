"""Threshold processor: an image becomes a label mask."""

from .processor import THRESHOLD_METHODS, ThresholdProcessor, threshold_labels

__all__ = ["THRESHOLD_METHODS", "ThresholdProcessor", "threshold_labels"]
