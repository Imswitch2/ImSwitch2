from slmcore import FocusedEditingPreferences,SLMStartupPreferences
from slmcore.application.startup_preferences import StartupPreferencesState


def test_focused_editing_preferences_roundtrip_and_normalize():
    selection = FocusedEditingPreferences(
        groups=("cgh","cgh"," corrections "),
        items={
            "patterns":("linear_phase","lens_phase","linear_phase"),
            "":("ignored",),
        },
    )

    assert selection.groups == ("cgh","corrections")
    assert selection.item_keys("patterns") == ("linear_phase","lens_phase")
    assert selection.selects_group("patterns")
    assert selection.configured
    assert FocusedEditingPreferences.from_value(selection.to_dict()) == selection


def test_startup_preferences_persist_one_focused_selection_per_section():
    selection = FocusedEditingPreferences(
        groups=("cgh",),
        items={"patterns":("linear_phase","lens_phase")},
    )
    preferences = SLMStartupPreferences(
        focused_editing={"sec_0":selection},
    )

    restored = SLMStartupPreferences.from_dict(preferences.to_dict())

    assert restored == preferences
    assert restored.focused_editing["sec_0"] == selection


def test_startup_preferences_state_can_set_and_clear_focused_selection():
    saved = []
    state = StartupPreferencesState(SLMStartupPreferences(),saved.append)
    selection = FocusedEditingPreferences(
        items={"patterns":("linear_phase",)},
    )

    state.set_focused_editing("sec_0",selection)
    assert state.focused_editing("sec_0") == selection
    assert saved[-1].focused_editing["sec_0"] == selection

    state.set_focused_editing("sec_0",FocusedEditingPreferences())
    assert not state.focused_editing("sec_0").configured
    assert "sec_0" not in saved[-1].focused_editing


def test_old_startup_preferences_default_to_no_focused_selection():
    restored = SLMStartupPreferences.from_dict({
        "startup_config":None,
        "default_mappings":{},
        "section_display_mode":"tabs",
    })

    assert dict(restored.focused_editing) == {}
