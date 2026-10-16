import pytest

from slmcore import SLMSectionAddress


def test_section_address_normalizes_keys_and_is_hashable():
    address = SLMSectionAddress("  slm_a  ","  section_0  ")

    assert address.slm_key == "slm_a"
    assert address.section_key == "section_0"
    assert {address: "value"}[SLMSectionAddress("slm_a","section_0")] == "value"


def test_section_address_rejects_empty_keys():
    with pytest.raises(ValueError,match="slm_key"):
        SLMSectionAddress("   ","section_0")
    with pytest.raises(ValueError,match="section_key"):
        SLMSectionAddress("slm_a","   ")


def test_section_address_rejects_non_string_keys():
    with pytest.raises(TypeError,match="slm_key"):
        SLMSectionAddress(1,"section_0")
    with pytest.raises(TypeError,match="section_key"):
        SLMSectionAddress("slm_a",1)


def test_same_section_key_on_different_slms_is_unambiguous():
    first = SLMSectionAddress("slm_a","multi_foci")
    second = SLMSectionAddress("slm_b","multi_foci")

    assert first != second
    assert len({first,second}) == 2
