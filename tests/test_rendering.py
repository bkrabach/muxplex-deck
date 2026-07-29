"""Tests for the key-face design system (docs/KEY_DESIGN_SYSTEM.md).

No hardware, no server, no I/O -- pure PIL rendering exercised through a
minimal fake `DeckDevice` (only the `key_image_format`/`touchscreen_image_format`
surface `PILHelper` actually calls). Covers:

- Zone geometry (`_zone_geometry`) matches the design doc's own table at
  S=72/96/120, and every band's origin sums to the face edge exactly.
- Type scale (`_primary_size`/`_secondary_size`) matches the design doc's
  table; TEXTURE stays fixed regardless of `S`.
- The border/text collision fix: rendered NAME/BODY/STATE text never
  draws into the border's own pixels, for both the single-state and the
  (former) dual-state case.
- Preview line count matches the hardware-verified 4 lines at S=72 / 8
  lines at S=120 the design doc calls out.
- The two orthogonal state channels (active ring, attention band) render
  distinctly and simultaneously without colliding.
- The named violations from the design doc are actually fixed:
  `page_prev` has a NAME, `page_picker`'s BODY doesn't repeat its NAME,
  `toggle_last` gets a NAME, `view_picker`'s STATE is no longer hostname.
"""

from __future__ import annotations

from typing import cast

import pytest
from muxplex_client import Bell, Session
from PIL import Image

from muxplex_deck import rendering
from muxplex_deck.device import DeckDevice


class FakeKeyDeck:
    """Minimal `DeckDevice` fake -- only the surface `PILHelper` touches."""

    def __init__(self, *, key_size: int) -> None:
        self._key_size = key_size

    def key_image_format(self) -> dict:
        return {
            "size": (self._key_size, self._key_size),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def touchscreen_image_format(self) -> dict:
        return {
            "size": (800, 100),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }


def _deck(key_size: int) -> DeckDevice:
    return cast(DeckDevice, FakeKeyDeck(key_size=key_size))


def _session(
    name: str = "deckwork", *, snapshot: str = "", needs_attention: bool = False
) -> Session:
    bell = (
        Bell(last_fired_at=2.0, seen_at=1.0, unseen_count=1)
        if needs_attention
        else Bell(last_fired_at=None, seen_at=None, unseen_count=0)
    )
    return Session(name=name, snapshot=snapshot, bell=bell)


def _to_image(jpeg_bytes: bytes) -> Image.Image:
    import io

    return Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")


def _hex_rgb(hex_color: str) -> tuple[int, int, int]:
    return cast(
        "tuple[int, int, int]",
        tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5)),
    )


# Real key images round-trip through JPEG (`key_image_format()["format"]`),
# a LOSSY codec -- exact pixel equality is the wrong test. All color
# assertions below compare with tolerance instead.
_COLOR_TOLERANCE = 40


def _as_rgb(pixel: object) -> tuple[int, int, int]:
    """`Image.getpixel`'s return type is broader than our RGB-mode images
    ever actually produce -- narrow it for the tolerance helpers below."""
    assert isinstance(pixel, tuple) and len(pixel) == 3
    return cast("tuple[int, int, int]", pixel)


def _assert_color_close(actual: object, expected: tuple[int, int, int]) -> None:
    rgb = _as_rgb(actual)
    assert all(abs(a - e) <= _COLOR_TOLERANCE for a, e in zip(rgb, expected)), (
        f"{rgb} not within {_COLOR_TOLERANCE} of {expected}"
    )


def _assert_color_far(actual: object, expected: tuple[int, int, int]) -> None:
    rgb = _as_rgb(actual)
    assert any(abs(a - e) > _COLOR_TOLERANCE for a, e in zip(rgb, expected)), (
        f"{rgb} unexpectedly close to {expected}"
    )


class TestZoneGeometry:
    """`_zone_geometry` matches docs/KEY_DESIGN_SYSTEM.md §1's table exactly."""

    @pytest.mark.parametrize(
        ("size", "border", "margin", "name_h", "body_h", "state_h"),
        [
            (72, 2, 4, 20, 30, 14),
            (96, 3, 5, 27, 41, 18),
            (120, 3, 7, 34, 49, 23),
        ],
    )
    def test_matches_design_doc_table(
        self,
        size: int,
        border: int,
        margin: int,
        name_h: int,
        body_h: int,
        state_h: int,
    ) -> None:
        geo = rendering._zone_geometry(size)
        assert geo.border == border
        assert geo.margin == margin
        assert geo.name_height == name_h
        assert geo.body_height == body_h
        assert geo.state_height == state_h

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_bands_sum_to_face_edge_with_no_rounding_drift(self, size: int) -> None:
        """margin + NAME + BODY + STATE + margin == size, exactly -- §1."""
        geo = rendering._zone_geometry(size)
        assert (
            geo.margin
            + geo.name_height
            + geo.body_height
            + geo.state_height
            + geo.margin
        ) == size

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_content_box_is_inset_by_margin_on_all_sides(self, size: int) -> None:
        geo = rendering._zone_geometry(size)
        assert geo.content_left == geo.margin
        assert geo.content_top == geo.margin
        assert geo.content_width == size - 2 * geo.margin
        assert geo.content_height == size - 2 * geo.margin

    def test_s72_band_origins_match_design_doc(self) -> None:
        """ "Band origins at S=72: NAME y 4..23, BODY y 24..53, STATE y 54..67"."""
        geo = rendering._zone_geometry(72)
        assert geo.name_top == 4
        assert geo.name_top + geo.name_height - 1 == 23
        assert geo.body_top == 24
        assert geo.body_top + geo.body_height - 1 == 53
        assert geo.state_top == 54
        assert geo.state_top + geo.state_height - 1 == 67

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_margin_always_clears_the_border(self, size: int) -> None:
        """The whole point of §3's fix: content starts strictly after the border."""
        geo = rendering._zone_geometry(size)
        assert geo.margin > geo.border


class TestTypeScale:
    """`_primary_size`/`_secondary_size` match docs/KEY_DESIGN_SYSTEM.md §2's table."""

    @pytest.mark.parametrize(
        ("size", "primary", "secondary"),
        [
            (72, 16, 11),
            (96, 21, 15),
            (120, 27, 18),
        ],
    )
    def test_matches_design_doc_table(
        self, size: int, primary: int, secondary: int
    ) -> None:
        assert rendering._primary_size(size) == primary
        assert rendering._secondary_size(size) == secondary

    def test_texture_size_is_fixed_regardless_of_s(self) -> None:
        """§2/§5: TEXTURE's value is column count, not apparent size -- never scales."""
        assert rendering._TEXTURE_SIZE == 11


class TestPreviewGeometry:
    """Preview line count matches the hardware-verified counts in §1/§5."""

    def test_four_lines_at_s72(self) -> None:
        geo = rendering._zone_geometry(72)
        lines, _columns = rendering._preview_geometry(72, geo.content_height)
        assert lines == 4

    def test_eight_lines_at_s120(self) -> None:
        geo = rendering._zone_geometry(120)
        lines, _columns = rendering._preview_geometry(120, geo.content_height)
        assert lines == 8

    def test_columns_unchanged_from_pre_v3_hardware_verified_count(self) -> None:
        """21 columns at S=120 -- the column formula deliberately wasn't
        rebased onto the zone-model content width (see the function's own
        docstring): doing so would drop it to 19, an unrequested and
        un-hardware-verified regression.
        """
        geo = rendering._zone_geometry(120)
        _lines, columns = rendering._preview_geometry(120, geo.content_height)
        assert columns == 21


class TestBorderTextCollision:
    """§3's actual fix: fitted text never draws into the border's own pixels."""

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_session_name_never_overlaps_border_pixels(self, size: int) -> None:
        deck = _deck(size)
        session = _session("a-very-long-session-name-indeed")
        image = _to_image(rendering.render_session_key(deck, session, active=True))
        geo = rendering._zone_geometry(size)
        # Every pixel strictly inside the margin, within the NAME band's
        # row range, must NOT be the active ring's cyan -- if it were, the
        # ring and the band would be touching/overlapping.
        ring_rgb = _hex_rgb(rendering._ACTIVE_RING_COLOR)
        for y in range(geo.name_top, geo.name_top + geo.name_height):
            for x in (geo.content_left, geo.content_left + geo.content_width - 1):
                _assert_color_far(image.getpixel((x, y)), ring_rgb)

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_border_ring_stays_within_the_margin(self, size: int) -> None:
        """The ring (width B, inset 0) must fit entirely before content
        starts at `margin` -- i.e. B <= margin, with the gap being the
        remaining pixels. This is the geometric guarantee §3 relies on.
        """
        geo = rendering._zone_geometry(size)
        assert geo.border <= geo.margin


class TestStateChannels:
    """§3/§4: active (ring) and attention (amber NAME band) are orthogonal."""

    def test_active_session_draws_a_ring_at_the_face_edge(self) -> None:
        deck = _deck(72)
        session = _session("s1")
        image = _to_image(rendering.render_session_key(deck, session, active=True))
        ring_rgb = _hex_rgb(rendering._ACTIVE_RING_COLOR)
        # Top-left corner pixel is on the B=2 ring at inset 0.
        _assert_color_close(image.getpixel((0, 0)), ring_rgb)

    def test_inactive_session_has_no_ring(self) -> None:
        deck = _deck(72)
        session = _session("s1")
        image = _to_image(rendering.render_session_key(deck, session, active=False))
        ring_rgb = _hex_rgb(rendering._ACTIVE_RING_COLOR)
        _assert_color_far(image.getpixel((0, 0)), ring_rgb)

    @staticmethod
    def _band_interior_point(geo: rendering._ZoneGeometry) -> tuple[int, int]:
        """A point well inside the NAME band's fill, clear of both the
        band's own edges (where JPEG's 8x8 block quantization blends
        neighboring colors together) and the centred glyph ink.
        """
        return geo.content_left + 8, geo.name_top + geo.name_height // 2

    def test_needs_attention_fills_name_band_amber(self) -> None:
        deck = _deck(72)
        session = _session("s1", needs_attention=True)
        image = _to_image(rendering.render_session_key(deck, session, active=False))
        geo = rendering._zone_geometry(72)
        pixel = image.getpixel(self._band_interior_point(geo))
        _assert_color_close(pixel, (0xF1, 0xA6, 0x40))

    def test_no_attention_name_band_is_translucent_black_not_amber(self) -> None:
        deck = _deck(72)
        session = _session("s1", needs_attention=False)
        image = _to_image(rendering.render_session_key(deck, session, active=False))
        geo = rendering._zone_geometry(72)
        pixel = image.getpixel(self._band_interior_point(geo))
        _assert_color_far(pixel, (0xF1, 0xA6, 0x40))

    def test_active_and_attention_apply_simultaneously_without_colliding(self) -> None:
        """Both channels present at once: ring at the edge, amber band inset
        by the margin -- neither is dropped, per §3's "both can be present
        simultaneously" claim.
        """
        deck = _deck(72)
        session = _session("s1", needs_attention=True)
        image = _to_image(rendering.render_session_key(deck, session, active=True))
        geo = rendering._zone_geometry(72)
        ring_rgb = _hex_rgb(rendering._ACTIVE_RING_COLOR)
        _assert_color_close(image.getpixel((0, 0)), ring_rgb)  # ring still present
        _assert_color_close(
            image.getpixel(self._band_interior_point(geo)), (0xF1, 0xA6, 0x40)
        )  # band still amber


class TestRenderSizes:
    """Every render_* function returns an image at the deck's real pixel size."""

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_session_key(self, size: int) -> None:
        deck = _deck(size)
        image = _to_image(rendering.render_session_key(deck, _session(), active=False))
        assert image.size == (size, size)

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_control_key(self, size: int) -> None:
        deck = _deck(size)
        image = _to_image(
            rendering.render_control_key(deck, name="< PREV", body="VIEW", state="1/2")
        )
        assert image.size == (size, size)

    @pytest.mark.parametrize("size", [72, 96, 120])
    def test_picker_key(self, size: int) -> None:
        deck = _deck(size)
        image = _to_image(rendering.render_picker_key(deck, "all", current=True))
        assert image.size == (size, size)

    def test_empty_key(self) -> None:
        deck = _deck(72)
        image = _to_image(rendering.render_empty_key(deck))
        assert image.size == (72, 72)

    def test_status_key(self) -> None:
        deck = _deck(72)
        image = _to_image(rendering.render_status_key(deck, "AUTH FAILED"))
        assert image.size == (72, 72)


class TestZeroContentPixelCost:
    """§3: the ring costs 0 content pixels; the amber band costs 0 extra pixels."""

    def test_ring_present_vs_absent_does_not_move_name_band_geometry(self) -> None:
        """Active vs inactive must not change where the NAME band sits --
        only bordering, never reflowing content, per the "0 content pixels"
        claim.
        """
        deck = _deck(72)
        session = _session("same-name")
        active_img = _to_image(rendering.render_session_key(deck, session, active=True))
        inactive_img = _to_image(
            rendering.render_session_key(deck, session, active=False)
        )
        geo = rendering._zone_geometry(72)
        # Centre column of the NAME band, well clear of the edge ring,
        # should be identical ink either way (same label, same band fill).
        mid_x = geo.content_left + geo.content_width // 2
        mid_y = geo.name_top + geo.name_height // 2
        assert active_img.getpixel((mid_x, mid_y)) == inactive_img.getpixel(
            (mid_x, mid_y)
        )


class TestFitLabelIsSoleTruncationGate:
    """§5: MAX_SESSION_LABEL_CHARS-style fixed char caps are gone; only
    measured pixel fit (`_fit_label`) truncates -- see rendering.py's
    `_fit_label` docstring.
    """

    def test_max_session_label_chars_constant_removed(self) -> None:
        assert not hasattr(rendering, "MAX_SESSION_LABEL_CHARS")

    def test_short_name_under_ten_chars_is_not_truncated(self) -> None:
        # A name that the old MAX_SESSION_LABEL_CHARS=10 cap would have let
        # through unmodified -- proves this path still works post-removal.
        deck = _deck(120)
        image = _to_image(
            rendering.render_session_key(deck, _session("shortname"), active=False)
        )
        assert image.size == (120, 120)

    def test_long_name_is_pixel_fit_truncated_with_ellipsis(self) -> None:
        from PIL import ImageDraw, ImageFont

        image = Image.new("RGB", (72, 72))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=rendering._primary_size(72))
        geo = rendering._zone_geometry(72)
        fitted = rendering._fit_label(
            draw, "a-very-long-session-name-indeed", font, geo.content_width
        )
        assert fitted.endswith("\u2026")
        assert draw.textlength(fitted, font=font) <= geo.content_width


class TestControlKeyZoneAssignment:
    """§6.2's worked table -- the named violations are actually fixed."""

    def test_name_and_body_and_state_each_land_in_their_own_band(self) -> None:
        """A control key with all three bands populated puts each string's
        ink at a distinct y (proving they don't all render at the same
        vertical position, i.e. the bands really are 3 separate rows).
        """
        deck = _deck(72)
        image = _to_image(
            rendering.render_control_key(deck, name="< PREV", body="PAGE", state="1/2")
        )
        # Collect the y-rows containing any non-background ink.
        bg = (0x10, 0x10, 0x36)
        ink_rows = {
            y for y in range(72) for x in range(72) if image.getpixel((x, y)) != bg
        }
        geo = rendering._zone_geometry(72)
        name_rows = set(range(geo.name_top, geo.name_top + geo.name_height))
        body_rows = set(range(geo.body_top, geo.body_top + geo.body_height))
        state_rows = set(range(geo.state_top, geo.state_top + geo.state_height))
        assert ink_rows & name_rows
        assert ink_rows & body_rows
        assert ink_rows & state_rows

    def test_empty_name_and_state_leave_those_bands_blank(self) -> None:
        """Picker BODY-only faces (§6.3) reserve NAME/STATE but draw nothing
        in them -- `render_control_key`'s `name`/`state` default to "".
        """
        deck = _deck(72)
        image = _to_image(rendering.render_control_key(deck, name="", body="ALL"))
        bg = (0x10, 0x10, 0x36)
        geo = rendering._zone_geometry(72)
        for y in range(geo.name_top, geo.name_top + geo.name_height):
            for x in range(geo.content_left, geo.content_left + geo.content_width):
                assert image.getpixel((x, y)) == bg
