"""The Config Studio's offline plugin lists must mirror the real registries.

The Studio can run against an ImProcess that is not importable -- a partial
install, or a plugin whose own imports fail -- so it carries a built-in copy of
the ImProcess plugin lists. That copy had drifted three reconstructors and
nineteen processors behind, and because the import failure is caught, the
operator was shown a short list with no indication anything was missing.
"""

from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor

_EDITOR_DIR = Path(editor.__file__).resolve().parent


def test_reconstructor_fallback_matches_the_registry():
    from imswitch.improcess.reconstructors import available_reconstructor_ids

    assert editor.IMPROCESS_RECONSTRUCTOR_FALLBACK == available_reconstructor_ids()


def test_processor_fallback_matches_the_registry():
    from imswitch.improcess.processors import available_processor_ids

    assert editor.IMPROCESS_PROCESSOR_FALLBACK == available_processor_ids()


def test_tiling_reconstructor_is_offered():
    """The specific plugin whose absence exposed the drift."""
    assert 'tiling-mosaic' in editor.IMPROCESS_RECONSTRUCTOR_FALLBACK


def test_placeholders_resolve_to_the_live_registry():
    from imswitch.improcess.reconstructors import available_reconstructor_ids

    schema = {"fields": [
        {"key": "reconstructors", "opts": ["__improcess_reconstructors__"]},
        {"key": "processors", "opts": ["__improcess_processors__"]},
    ]}
    editor._resolve_section_dynamic_options(schema)

    assert schema["fields"][0]["opts"] == available_reconstructor_ids()
    assert editor.PLUGIN_DISCOVERY_ERROR is None


def test_fallback_is_used_and_reported_when_improcess_is_unavailable(
    monkeypatch, capsys
):
    """A failed import must degrade visibly, not silently."""
    import builtins

    realImport = builtins.__import__

    def _blockImProcess(name, *args, **kwargs):
        if name.startswith('imswitch.improcess'):
            raise ImportError('improcess not installed')
        return realImport(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', _blockImProcess)
    monkeypatch.setattr(editor, 'PLUGIN_DISCOVERY_ERROR', None)

    schema = {"fields": [
        {"key": "reconstructors", "opts": ["__improcess_reconstructors__"]},
    ]}
    editor._resolve_section_dynamic_options(schema)

    assert schema["fields"][0]["opts"] == editor.IMPROCESS_RECONSTRUCTOR_FALLBACK
    assert editor.PLUGIN_DISCOVERY_ERROR is not None
    assert 'built-in list' in capsys.readouterr().err

    # Leave the module in the state the other tests expect.
    monkeypatch.setattr(editor, 'PLUGIN_DISCOVERY_ERROR', None)


def test_processing_template_uses_the_placeholder():
    """The template must defer to discovery rather than hardcode a list."""
    import json

    template = (
        _EDITOR_DIR / "builtin_templates" / "sections" / "processing.json"
    )
    fields = json.loads(template.read_text(encoding="utf-8")).get("fields", [])
    byKey = {field.get("key"): field for field in fields}

    assert byKey["reconstructors"]["opts"] == ["__improcess_reconstructors__"]
