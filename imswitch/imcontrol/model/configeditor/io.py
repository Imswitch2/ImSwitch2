"""File I/O helpers for config editor.

This module provides Qt-free functions for loading and saving configuration files.
The save preparation step ensures clean JSON output while preserving all user data.
"""

import json
import copy
from pathlib import Path


def load_config_file(path: str) -> dict:
    """Load a configuration file and return its data as a dict.
    
    Args:
        path: Path to the JSON configuration file
    
    Returns:
        The parsed configuration data
    
    Raises:
        FileNotFoundError: If the file doesn't exist
        json.JSONDecodeError: If the file is not valid JSON
        Exception: For other I/O errors
    """
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def prepare_for_save(data: dict) -> dict:
    """Prepare configuration data for saving.
    
    This function deep-copies the data and applies the minimal transformations
    needed for clean JSON output:
    - Removes empty "others" dict (cosmetic cleanup only)
    - Preserves ALL other keys, including unknown sections and fields
    
    Args:
        data: The configuration data dict
    
    Returns:
        A deep copy of the data with empty "others" removed if present.
    """
    result = copy.deepcopy(data)
    
    # Strip empty "others" dict to keep saved JSON clean
    # Non-empty "others" is preserved (it contains real devices)
    if "others" in result and not result["others"]:
        del result["others"]
    
    return result
