"""Coordinate conventions shared by CGH targets and parameter converters."""

from __future__ import annotations


FOURIER_REFERENCE_SIZE = 512


def reference_px_to_k(value):
    """Convert fixed-reference Fourier pixels to cycles per SLM pixel."""
    return float(value) / float(FOURIER_REFERENCE_SIZE)


def k_to_reference_px(value):
    """Convert cycles per SLM pixel to fixed-reference Fourier pixels."""
    return float(value) * float(FOURIER_REFERENCE_SIZE)
