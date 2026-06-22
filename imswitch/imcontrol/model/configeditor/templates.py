"""Plugin template loader for the config editor.

This module provides a pure-Python (no Qt dependencies) service for loading
setup templates declared by plugin contributions via importlib.resources.
"""

import importlib.resources
import json
import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginTemplate:
    """A successfully-loaded plugin template."""
    
    name: str            # display name (file stem, or a "name" field if present)
    category: str        # editor category from the owning ManagerInfo
    manager_name: str    # owning manager id
    plugin_name: str     # source plugin label, e.g. "vendor-superstage"
    source_package: str
    resource: str        # the relative resource path
    device: dict         # parsed device dict


@dataclass(frozen=True)
class PluginTemplateError:
    """An error encountered while loading a plugin template."""
    
    manager_name: str
    plugin_name: str | None
    source_package: str | None
    resource: str
    message: str         # human-readable reason (file missing, bad JSON, not a device)


def load_plugin_templates(
    catalog: "ManagerCatalog",
) -> tuple[list[PluginTemplate], list[PluginTemplateError]]:
    """Load all setup_templates declared by catalog managers via importlib.resources.
    
    Args:
        catalog: A ManagerCatalog instance from which to load templates.
    
    Returns:
        A tuple of (templates, errors). Never raises for a bad template — collects
        errors instead.
    """
    logger = logging.getLogger(__name__)
    templates: list[PluginTemplate] = []
    errors: list[PluginTemplateError] = []
    
    # Iterate through all managers in the catalog
    for manager_info in catalog.managers():
        # Skip managers without templates or source package
        if not manager_info.setup_templates or not manager_info.source_package:
            continue
        
        # Process each declared template resource
        for resource_path in manager_info.setup_templates:
            try:
                # Resolve the resource via importlib.resources
                resource = (
                    importlib.resources.files(manager_info.source_package)
                    / resource_path
                )
                
                # Read and parse the JSON
                template_text = resource.read_text(encoding="utf-8")
                template_data = json.loads(template_text)
                
                # Validate that it's a device dict with managerName
                if not isinstance(template_data, dict):
                    errors.append(PluginTemplateError(
                        manager_name=manager_info.manager_name,
                        plugin_name=manager_info.plugin_name,
                        source_package=manager_info.source_package,
                        resource=resource_path,
                        message=f"Template is not a dict (got {type(template_data).__name__})"
                    ))
                    continue
                
                if "managerName" not in template_data:
                    errors.append(PluginTemplateError(
                        manager_name=manager_info.manager_name,
                        plugin_name=manager_info.plugin_name,
                        source_package=manager_info.source_package,
                        resource=resource_path,
                        message="Template dict missing required 'managerName' field"
                    ))
                    continue
                
                # Determine display name: use a metadata "name" field if present,
                # else the file stem. When "name" is used as the label it is
                # metadata, not a device field, so drop it from the device dict
                # that gets instantiated into the config.
                device = template_data
                if "name" in template_data and isinstance(template_data["name"], str):
                    display_name = template_data["name"]
                    device = {k: v for k, v in template_data.items() if k != "name"}
                else:
                    # Use file stem as display name
                    display_name = Path(resource_path).stem

                # Successfully loaded
                templates.append(PluginTemplate(
                    name=display_name,
                    category=manager_info.category,
                    manager_name=manager_info.manager_name,
                    plugin_name=manager_info.plugin_name or "unknown",
                    source_package=manager_info.source_package,
                    resource=resource_path,
                    device=device
                ))
                logger.debug(
                    f"Loaded template '{display_name}' from {manager_info.plugin_name} "
                    f"({resource_path})"
                )
                
            except FileNotFoundError:
                errors.append(PluginTemplateError(
                    manager_name=manager_info.manager_name,
                    plugin_name=manager_info.plugin_name,
                    source_package=manager_info.source_package,
                    resource=resource_path,
                    message=f"Template resource not found: {resource_path}"
                ))
            except json.JSONDecodeError as e:
                errors.append(PluginTemplateError(
                    manager_name=manager_info.manager_name,
                    plugin_name=manager_info.plugin_name,
                    source_package=manager_info.source_package,
                    resource=resource_path,
                    message=f"JSON parse error: {e}"
                ))
            except (ImportError, ModuleNotFoundError) as e:
                errors.append(PluginTemplateError(
                    manager_name=manager_info.manager_name,
                    plugin_name=manager_info.plugin_name,
                    source_package=manager_info.source_package,
                    resource=resource_path,
                    message=f"Package import error: {e}"
                ))
            except Exception as e:
                # Catch-all for any other errors
                errors.append(PluginTemplateError(
                    manager_name=manager_info.manager_name,
                    plugin_name=manager_info.plugin_name,
                    source_package=manager_info.source_package,
                    resource=resource_path,
                    message=f"Unexpected error: {type(e).__name__}: {e}"
                ))
    
    # Sort templates deterministically: by plugin_name, then category, then name
    templates.sort(key=lambda t: (t.plugin_name, t.category, t.name))
    
    # Sort errors similarly for consistent display
    errors.sort(key=lambda e: (e.plugin_name or "", e.manager_name, e.resource))
    
    logger.info(
        f"Loaded {len(templates)} plugin template(s) with {len(errors)} error(s)"
    )
    
    return templates, errors
