"""Physical-deck appearance configuration and rendering tests.

These tests are intentionally local-only: no Stream Deck, sidecar, service,
or muxplex server is involved.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PIL import Image, ImageDraw, ImageFont

from muxplex_deck import cli, rendering
from muxplex_deck.appearance import (
    DEFAULT_APPEARANCE,
    MIN_ATTENTION_TEXT_CONTRAST,
    _contrast_ratio,
)
from muxplex_deck.config import ConfigError, ConfigWatcher, load_config, load_raw_config
from muxplex_deck.device import DeckDevice


class _Deck:
    def key_image_format(self) -> dict:
        return {
            "size": (72, 72),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }


def _deck() -> DeckDevice:
    return cast(DeckDevice, _Deck())


def _write_config(tmp_path: Path, appearance: object | None = None) -> Path:
    key = tmp_path / "federation_key"
    key.write_text("secret\n", encoding="utf-8")
    data: dict[str, object] = {
        "server_url": "https://example.test:8088",
        "key_file": str(key),
    }
    if appearance is not None:
        data["appearance"] = appearance
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestAppearanceValidation:
    def test_absent_appearance_uses_legacy_visual_defaults(
        self, tmp_path: Path
    ) -> None:
        config = load_config(str(_write_config(tmp_path)))
        assert config.appearance == DEFAULT_APPEARANCE
        assert (
            _contrast_ratio(
                config.appearance.palette.attention,
                config.appearance.palette.attention_text,
            )
            >= MIN_ATTENTION_TEXT_CONTRAST
        )

    @pytest.mark.parametrize(
        "appearance",
        [
            {
                "palette": {
                    "attention": "#000000",
                    "attention_text": "#000000",
                }
            },
            {
                "palette": {
                    "attention": "#777777",
                    "attention_text": "#888888",
                }
            },
        ],
        ids=["same-colors", "low-contrast-colors"],
    )
    def test_insufficient_attention_text_contrast_fails_closed(
        self, tmp_path: Path, appearance: object
    ) -> None:
        with pytest.raises(ConfigError, match="contrast ratio"):
            load_config(str(_write_config(tmp_path, appearance)))

    @pytest.mark.parametrize(
        "appearance, expected",
        [
            ({"primary": {"scale": True}}, "primary.scale"),
            ({"secondary": {"color": "#12GG34"}}, "secondary.color"),
            ({"preview": {"scale": 2.01}}, "preview.scale"),
            ({"palette": {"bogus": "#123456"}}, "palette"),
            ({"unknown": {}}, "unknown group"),
        ],
    )
    def test_invalid_appearance_fails_closed(
        self, tmp_path: Path, appearance: object, expected: str
    ) -> None:
        with pytest.raises(ConfigError, match=expected):
            load_config(str(_write_config(tmp_path, appearance)))

    def test_partial_appearance_merges_defaults_and_normalizes_color(
        self, tmp_path: Path
    ) -> None:
        config = load_config(
            str(_write_config(tmp_path, {"primary": {"color": "#a1b2c3"}}))
        )
        assert config.appearance.primary.color == "#A1B2C3"
        assert config.appearance.primary.scale == 1.0
        assert config.appearance.preview == DEFAULT_APPEARANCE.preview

    @pytest.mark.parametrize(
        ("font_scale", "appearance", "expected"),
        [
            (1.25, {"primary": {"scale": 1.01}}, "appearance.primary.scale"),
            (1.0, {"secondary": {"scale": 1.26}}, "appearance.secondary.scale"),
            (2.0, None, "appearance.primary.scale"),
        ],
    )
    def test_readable_scale_product_that_exceeds_smallest_key_is_rejected(
        self,
        tmp_path: Path,
        font_scale: float,
        appearance: object | None,
        expected: str,
    ) -> None:
        path = _write_config(tmp_path, appearance)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["font_scale"] = font_scale
        path.write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigError, match=expected):
            load_config(str(path))

    def test_maximum_combined_readable_scale_fits_every_smallest_key_band(
        self, tmp_path: Path
    ) -> None:
        path = _write_config(
            tmp_path,
            {"primary": {"scale": 1.25}, "secondary": {"scale": 1.25}},
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        data["font_scale"] = 1.0
        path.write_text(json.dumps(data), encoding="utf-8")

        config = load_config(str(path))
        geo = rendering._zone_geometry(72)
        primary_size = rendering._primary_size(
            72, config.font_scale, config.appearance.primary.scale
        )
        secondary_size = rendering._secondary_size(
            72, config.font_scale, config.appearance.secondary.scale
        )
        primary_bbox = ImageFont.load_default(size=primary_size).getbbox("Hxg")
        assert primary_bbox[3] - primary_bbox[1] <= geo.name_height
        secondary_bbox = ImageFont.load_default(size=secondary_size).getbbox("Hxg")
        assert secondary_bbox[3] - secondary_bbox[1] <= geo.state_height


class TestAppearanceRendering:
    def test_default_config_appearance_is_pixel_identical_to_legacy_render(
        self, tmp_path: Path
    ) -> None:
        config = load_config(str(_write_config(tmp_path)))
        deck = _deck()
        legacy = rendering.render_control_key(
            deck, name="< PREV", body="PAGE", state="1/2"
        )
        configured = rendering.render_control_key(
            deck,
            name="< PREV",
            body="PAGE",
            state="1/2",
            appearance=config.appearance,
        )
        assert configured == legacy

    def test_palette_background_reaches_rendered_empty_key(
        self, tmp_path: Path
    ) -> None:
        config = load_config(
            str(_write_config(tmp_path, {"palette": {"empty_background": "#123456"}}))
        )
        image = Image.open(
            io.BytesIO(
                rendering.render_empty_key(_deck(), appearance=config.appearance)
            )
        ).convert("RGB")
        pixel = image.getpixel((36, 36))
        assert isinstance(pixel, tuple)
        assert all(
            abs(actual - expected) <= 20
            for actual, expected in zip(pixel, (18, 52, 86))
        )

    def test_preview_scale_scales_crop_metrics_with_its_font(self) -> None:
        lines, columns, line_height = rendering._scaled_preview_geometry(120, 106, 2.0)
        assert (lines, columns, line_height) == (4, 10, 26)
        assert rendering._preview_metrics(1.0) == (11, 13, 5.5)

    def test_role_scale_changes_primary_rendering_without_changing_global_scale(
        self, tmp_path: Path
    ) -> None:
        config = load_config(str(_write_config(tmp_path, {"primary": {"scale": 1.25}})))
        assert rendering._primary_size(72, 1.0, config.appearance.primary.scale) == 20

    def test_status_key_metrics_scale_line_height_and_line_capacity(self) -> None:
        geo = rendering._zone_geometry(72)
        default_size, default_line_height = rendering._status_key_metrics(72, 1.0)
        large_size, large_line_height = rendering._status_key_metrics(72, 1.25)

        assert (default_size, default_line_height) == (
            11,
            13,
        )
        assert (large_size, large_line_height) == (
            14,
            17,
        )
        assert (
            large_line_height
            >= ImageFont.load_default(size=large_size).getbbox("Hxg")[3]
        )
        assert 1 + (geo.content_height - 13) // default_line_height == 4
        assert 1 + (geo.content_height - 17) // large_line_height == 3

    def test_scaled_status_key_places_each_wrapped_line_inside_content_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        appearance = replace(
            DEFAULT_APPEARANCE,
            secondary=replace(DEFAULT_APPEARANCE.secondary, scale=1.25),
        )
        calls: list[
            tuple[
                tuple[float, float],
                ImageFont.FreeTypeFont | ImageFont.ImageFont,
            ]
        ] = []

        def record_text(
            draw: ImageDraw.ImageDraw,
            xy: tuple[float, float],
            text: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            font = cast(ImageFont.FreeTypeFont | ImageFont.ImageFont, kwargs["font"])
            calls.append((xy, font))

        monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
        rendering.render_status_key(
            _deck(),
            "alpha bravo charlie delta echo foxtrot",
            appearance=appearance,
        )

        geo = rendering._zone_geometry(72)
        assert len(calls) == 3
        for (x, y), font in calls:
            assert x == geo.content_left
            assert y >= geo.content_top
            assert y + font.getbbox("Hxg")[3] <= geo.content_top + geo.content_height


class TestAppearanceCli:
    def test_set_and_reset_safe_dotted_leaf(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        path = str(tmp_path / "config.json")

        assert cli.appearance_set("primary.scale", "1.25", path) == 0
        assert load_raw_config(path)["appearance"]["primary"]["scale"] == 1.25
        assert cli.appearance_reset("primary.scale", path) == 0
        assert load_raw_config(path)["appearance"]["primary"]["scale"] == 1.0
        assert "primary.scale reset to: 1.0" in capsys.readouterr().out

    def test_set_rejects_unknown_or_invalid_dotted_leaf(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        path = str(tmp_path / "config.json")
        assert cli.appearance_set("primary.weight", "600", path) == 1
        assert cli.appearance_set("palette.active", "cyan", path) == 1
        assert not Path(path).exists()
        assert "Unknown appearance field" in capsys.readouterr().err

    def test_set_rejects_combined_readable_scale_that_cannot_fit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        path = str(tmp_path / "config.json")

        assert cli.appearance_set("secondary.scale", "1.26", path) == 1
        assert not Path(path).exists()
        assert "font_scale" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("palette.attention", "#000000"),
            ("palette.attention_text", "#F0A640"),
        ],
        ids=["same-colors", "low-contrast-colors"],
    )
    def test_set_rejects_insufficient_attention_text_contrast(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        field: str,
        value: str,
    ) -> None:
        path = str(tmp_path / "config.json")

        assert cli.appearance_set(field, value, path) == 1
        assert not Path(path).exists()
        assert "contrast ratio" in capsys.readouterr().err

    def test_generic_config_set_refuses_appearance_mapping(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_set(
                "appearance", '{"primary": {"scale": 1.25}}', str(tmp_path / "c.json")
            )
        assert excinfo.value.code == 1

    def test_main_dispatches_appearance_show(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[str | None] = []
        monkeypatch.setattr(
            cli, "appearance_show", lambda path: calls.append(path) or 0
        )
        monkeypatch.setattr(
            "sys.argv",
            ["muxplex-deck", "--config", str(tmp_path / "c.json"), "appearance"],
        )
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert calls == [str(tmp_path / "c.json")]


class TestAppearanceHotReload:
    def test_appearance_change_is_reloadable(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        initial = load_config(str(path))
        watcher = ConfigWatcher(str(path), initial)

        data = json.loads(path.read_text(encoding="utf-8"))
        data["appearance"] = {"palette": {"active": "#AA00FF"}}
        path.write_text(json.dumps(data), encoding="utf-8")
        old_mtime = path.stat().st_mtime
        path.touch()
        # Filesystems may expose coarse mtimes; force an unequivocal change.
        import os

        os.utime(path, (old_mtime + 5, old_mtime + 5))

        outcome = watcher.poll()

        assert outcome.applied == ("appearance",)
        assert watcher.current.appearance.palette.active == "#AA00FF"
