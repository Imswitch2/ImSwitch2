from __future__ import annotations

from ...application.addressing import SLMSectionAddress


def slm_display_label(session) -> str:
    """Return a compact user-facing label for one live physical SLM."""
    identity=session.runtime.identity
    label=str(identity.display_name or identity.key).strip()
    serial=str(identity.serial_number or "").strip()
    if serial and serial not in label:
        return "%s · %s" % (label,serial)
    return label


def slm_physical_detail(session,*,slm_key: str | None=None) -> str:
    """Return stable physical identity text suitable for tooltips/details."""
    identity=session.runtime.identity
    key=str(slm_key or identity.key).strip()
    parts=["SLM: %s" % key]
    serial=str(identity.serial_number or "").strip()
    if serial:
        parts.append("Serial: %s" % serial)
    display=str(identity.display_name or "").strip()
    if display:
        parts.append("Display name: %s" % display)
    return "\n".join(parts)


def section_display_label(session,address: SLMSectionAddress) -> str:
    """Return the friendly label for an explicitly addressed live section."""
    if not isinstance(address,SLMSectionAddress):
        raise TypeError("address must be an SLMSectionAddress")
    title=address.section_key
    if address.section_key in session.runtime.section_keys:
        try:
            title=str(session.section_host.section_title(address.section_key))
        except (AttributeError,KeyError,RuntimeError):
            title=address.section_key
    return "%s / %s" % (slm_display_label(session),title)


def section_physical_detail(session,address: SLMSectionAddress) -> str:
    """Return exact physical binding text for an addressed section."""
    if not isinstance(address,SLMSectionAddress):
        raise TypeError("address must be an SLMSectionAddress")
    identity=session.runtime.identity
    lines=[
        "SLM: %s" % address.slm_key,
        "Section: %s" % address.section_key,
    ]
    serial=str(identity.serial_number or "").strip()
    if serial:
        lines.append("Serial: %s" % serial)
    title=address.section_key
    if address.section_key in session.runtime.section_keys:
        try:
            title=str(session.section_host.section_title(address.section_key))
        except (AttributeError,KeyError,RuntimeError):
            pass
    if title and title != address.section_key:
        lines.append("Section name: %s" % title)
    return "\n".join(lines)


__all__=[
    "section_display_label",
    "section_physical_detail",
    "slm_display_label",
    "slm_physical_detail",
]
