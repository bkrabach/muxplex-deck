"""Validated visual tokens supported by physical-deck rendering.

The roles deliberately match the soft deck's semantic type scale:
PRIMARY is the key's main label, SECONDARY is supporting/status text, and
PREVIEW is the terminal crop.  Hardware v1 exposes only capabilities its
bundled single-weight PIL font can actually render: bounded role scales and
opaque RGB colors.  Font family, weight, and italic controls do not exist.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

MIN_ROLE_SCALE = 0.5
MAX_ROLE_SCALE = 2.0
MIN_ATTENTION_TEXT_CONTRAST = 4.5

# The smallest supported 72px key has a 20px NAME band and a 14px STATE
# band.  PRIMARY's unscaled 16px font and SECONDARY's unscaled 11px font
# therefore both fit safely only through a combined multiplier of 1.25:
# `round(16 * 1.25) == 20` and `round(11 * 1.25) == 14`.  This protects
# every supported larger key because all bands grow from that minimum.
MAX_COMBINED_READABLE_SCALE = 1.25


@dataclass(frozen=True)
class Typography:
    """Scale and ink for one semantic text role."""

    scale: float
    color: str


@dataclass(frozen=True)
class Palette:
    """Non-typographic physical-deck colors."""

    session_background: str
    control_background: str
    empty_background: str
    active: str
    attention: str
    attention_text: str


@dataclass(frozen=True)
class Appearance:
    """All visual controls the physical-deck renderer implements in v1."""

    primary: Typography
    secondary: Typography
    preview: Typography
    palette: Palette


# These exact legacy values are intentional: an omitted appearance must render
# byte-for-byte like the pre-appearance physical deck.
DEFAULT_APPEARANCE = Appearance(
    primary=Typography(scale=1.0, color="#FFFFFF"),
    secondary=Typography(scale=1.0, color="#8888AA"),
    preview=Typography(scale=1.0, color="#7A7A7A"),
    palette=Palette(
        session_background="#0A0A0A",
        control_background="#101036",
        empty_background="#000000",
        active="#00D9F5",
        attention="#F1A640",
        attention_text="#000000",
    ),
)

DEFAULT_APPEARANCE_DATA: dict[str, dict[str, object]] = asdict(DEFAULT_APPEARANCE)
APPEARANCE_LEAVES: tuple[str, ...] = (
    "primary.scale",
    "primary.color",
    "secondary.scale",
    "secondary.color",
    "preview.scale",
    "preview.color",
    "palette.session_background",
    "palette.control_background",
    "palette.empty_background",
    "palette.active",
    "palette.attention",
    "palette.attention_text",
)
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")


class AppearanceError(ValueError):
    """Raised when an appearance value cannot safely reach the renderer."""


def appearance_to_dict(appearance: Appearance) -> dict[str, dict[str, object]]:
    """Return a JSON-ready copy of *appearance*."""

    return asdict(appearance)


def _validate_scale(role: str, value: object) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not MIN_ROLE_SCALE <= value <= MAX_ROLE_SCALE
    ):
        raise AppearanceError(
            f"Config field 'appearance.{role}.scale' must be a finite number in "
            f"[{MIN_ROLE_SCALE}, {MAX_ROLE_SCALE}], got {value!r}"
        )
    return float(value)


def _validate_color(path: str, value: object) -> str:
    if not isinstance(value, str) or not _COLOR_RE.fullmatch(value):
        raise AppearanceError(
            f"Config field 'appearance.{path}' must be a #RRGGBB color, got {value!r}"
        )
    return value.upper()


def _relative_luminance(color: str) -> float:
    """Return the WCAG relative luminance of a validated ``#RRGGBB`` color."""

    components = (int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))

    def linearize(component: float) -> float:
        if component <= 0.04045:
            return component / 12.92
        return ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = (linearize(component) for component in components)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG contrast ratio for two validated opaque RGB colors."""

    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter, darker = sorted((first_luminance, second_luminance), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _merged_group(
    raw: object, group: str, defaults: dict[str, object]
) -> dict[str, object]:
    """Merge an optional appearance group and reject every unknown shape."""

    if raw is None:
        return dict(defaults)
    if not isinstance(raw, dict):
        raise AppearanceError(
            f"Config field 'appearance.{group}' must be a JSON object, got "
            f"{type(raw).__name__}"
        )
    unknown = set(raw) - set(defaults)
    if unknown:
        raise AppearanceError(
            f"Config field 'appearance.{group}' has unknown field(s): "
            f"{', '.join(sorted(map(str, unknown)))}"
        )
    return {**defaults, **raw}


def validate_appearance(raw: object) -> Appearance:
    """Validate a partial appearance object and fill omitted values from defaults.

    The object is intentionally strict about names, types, finite bounded
    scales, and six-digit opaque RGB colors.  A partial object is allowed so a
    hand-edited config can change one leaf without duplicating defaults.
    """

    if raw is None:
        return DEFAULT_APPEARANCE
    if not isinstance(raw, dict):
        raise AppearanceError(
            f"Config field 'appearance' must be a JSON object, got {type(raw).__name__}"
        )
    unknown = set(raw) - set(DEFAULT_APPEARANCE_DATA)
    if unknown:
        raise AppearanceError(
            "Config field 'appearance' has unknown group(s): "
            f"{', '.join(sorted(map(str, unknown)))}"
        )

    primary = _merged_group(
        raw.get("primary"), "primary", DEFAULT_APPEARANCE_DATA["primary"]
    )
    secondary = _merged_group(
        raw.get("secondary"), "secondary", DEFAULT_APPEARANCE_DATA["secondary"]
    )
    preview = _merged_group(
        raw.get("preview"), "preview", DEFAULT_APPEARANCE_DATA["preview"]
    )
    palette = _merged_group(
        raw.get("palette"), "palette", DEFAULT_APPEARANCE_DATA["palette"]
    )

    attention = _validate_color("palette.attention", palette["attention"])
    attention_text = _validate_color(
        "palette.attention_text", palette["attention_text"]
    )
    contrast = _contrast_ratio(attention, attention_text)
    if contrast < MIN_ATTENTION_TEXT_CONTRAST:
        raise AppearanceError(
            "Config fields 'appearance.palette.attention' and "
            "'appearance.palette.attention_text' must have a contrast ratio "
            f"of at least {MIN_ATTENTION_TEXT_CONTRAST:g}:1, got {contrast:.2f}:1"
        )

    return Appearance(
        primary=Typography(
            scale=_validate_scale("primary", primary["scale"]),
            color=_validate_color("primary.color", primary["color"]),
        ),
        secondary=Typography(
            scale=_validate_scale("secondary", secondary["scale"]),
            color=_validate_color("secondary.color", secondary["color"]),
        ),
        preview=Typography(
            scale=_validate_scale("preview", preview["scale"]),
            color=_validate_color("preview.color", preview["color"]),
        ),
        palette=Palette(
            session_background=_validate_color(
                "palette.session_background", palette["session_background"]
            ),
            control_background=_validate_color(
                "palette.control_background", palette["control_background"]
            ),
            empty_background=_validate_color(
                "palette.empty_background", palette["empty_background"]
            ),
            active=_validate_color("palette.active", palette["active"]),
            attention=attention,
            attention_text=attention_text,
        ),
    )


def validate_readable_type_scales(font_scale: float, appearance: Appearance) -> None:
    """Reject readable role scales that cannot fit the smallest key's bands.

    PRIMARY and SECONDARY sizes are each the product of the global
    ``font_scale`` and their appearance role scale.  Validating them
    independently permits an unsafe product (for example ``2.0 * 2.0``)
    which lets one label paint through the fixed NAME/BODY/STATE boundaries
    on a 72px key.  PREVIEW remains intentionally independent: it is texture,
    not a readable label, and its renderer scales its crop metrics with it.
    """

    for role, role_scale in (
        ("primary", appearance.primary.scale),
        ("secondary", appearance.secondary.scale),
    ):
        combined = font_scale * role_scale
        if combined > MAX_COMBINED_READABLE_SCALE:
            raise AppearanceError(
                "Config fields 'font_scale' and "
                f"'appearance.{role}.scale' combine to {combined:g}; the "
                f"maximum for readable {role} text is "
                f"{MAX_COMBINED_READABLE_SCALE:g} on the smallest supported key."
            )
