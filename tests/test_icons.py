"""Brand identity, monochrome extraction, and colour rendering without widgets."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import cairo
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GLib, Gio, GdkPixbuf

from luch.catalog import AppEntry
from luch.icons import application_asset, load_app_mask, monochrome_mask, paint_app_icon


def pixbuf(rows):
    height, width = len(rows), len(rows[0])
    data = bytes(channel for row in rows for pixel in row for channel in pixel)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB,
                                         True, 8, width, height, width * 4)


def values(mask):
    data, stride = mask.get_data(), mask.get_stride()
    return [data[y * stride + x] for y in range(mask.get_height()) for x in range(mask.get_width())]


class AppIconTest(unittest.TestCase):
    def test_prominent_applications_have_individual_portable_vectors(self):
        expected = {
            "google-chrome.desktop": "googlechrome", "discord.desktop": "discord",
            "org.telegram.desktop.desktop": "telegram", "spotify.desktop": "spotify",
            "steam.desktop": "steam", "chatgpt.desktop": "openai",
            "Counter-Strike 2.desktop": "counterstrike", "minecraft-tui.desktop": "minecraft",
            "kitty.desktop": "kitty", "thunar.desktop": "thunar",
            "foot.desktop": "foot",
            "nvim.desktop": "neovim", "vim.desktop": "vim",
            "org.prismlauncher.PrismLauncher.desktop": "prism",
            "org.openrgb.OpenRGB.desktop": "openrgb", "jconsole-java25-openjdk.desktop": "java",
        }
        rendered = set()
        for app_id, asset in expected.items():
            with self.subTest(app_id=app_id):
                entry = AppEntry(app_id, app_id)
                path = application_asset(entry)
                self.assertEqual(path.stem, asset)
                self.assertTrue(path.is_file())
                mask = load_app_mask(entry)
                self.assertIsNotNone(mask)
                self.assertGreater(max(values(mask)), 200)
                rendered.add(hashlib.sha256(bytes(mask.get_data())).digest())
        # Applications in the same category retain visibly different logos.
        self.assertEqual(len(rendered), len(expected))

    def test_wrapper_names_and_category_words_never_choose_other_app_logos(self):
        entry = AppEntry("my-tool.desktop", "Code file terminal browser", executable="discord-kitty-thunar-wrapper")
        self.assertIsNone(application_asset(entry))
        entry = AppEntry("discord.desktop", "Discord", executable="chrome-window-mode")
        self.assertEqual(application_asset(entry).stem, "discord")

    def test_explicit_custom_icon_takes_precedence_over_bundled_logo(self):
        entry = AppEntry("google-chrome.desktop", "Работа", icon_override="my-browser")
        self.assertIsNone(application_asset(entry))

    def test_opaque_white_background_is_removed_instead_of_becoming_square(self):
        rows = [[(255, 255, 255, 255) for _ in range(8)] for _ in range(8)]
        for y in range(2, 6):
            for x in range(2, 6):
                if x in (3, 4) or y in (3, 4):
                    rows[y][x] = (0, 0, 0, 255)
        mask = monochrome_mask(pixbuf(rows))
        self.assertEqual((mask.get_width(), mask.get_height()), (4, 4))
        output = values(mask)
        self.assertEqual(output[0], 0)
        self.assertEqual(output[5], 255)
        self.assertEqual(output[-1], 0)

    def test_transparent_monochrome_logo_preserves_holes_and_alpha(self):
        rows = [[(0, 0, 0, 0) for _ in range(8)] for _ in range(8)]
        for y in range(1, 7):
            for x in range(1, 7):
                if x in (1, 6) or y in (1, 6):
                    rows[y][x] = (200, 100, 50, 255)
        mask = monochrome_mask(pixbuf(rows))
        self.assertEqual((mask.get_width(), mask.get_height()), (6, 6))
        self.assertEqual(values(mask)[0], 255)
        self.assertEqual(values(mask)[7], 0)

    def test_similar_luminance_brand_colours_still_preserve_interior_shape(self):
        rows = [[(220, 0, 0, 255) for _ in range(6)] for _ in range(6)]
        for y in range(1, 5):
            rows[y][2] = (0, 0, 220, 255)
        mask = monochrome_mask(pixbuf(rows))
        self.assertEqual((mask.get_width(), mask.get_height()), (1, 4))
        self.assertEqual(values(mask), [255] * 4)

    def test_flat_opaque_tile_has_no_fake_square_logo(self):
        self.assertIsNone(monochrome_mask(pixbuf([[(20, 40, 80, 255)] * 8 for _ in range(8)])))

    def test_css_colour_is_applied_without_retaining_brand_colour(self):
        entry = AppEntry("discord.desktop", "Discord")
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 40)
        context = cairo.Context(surface)
        colour = (.25, .75, .5, 1)
        paint_app_icon(context, entry, 4, 4, 32, colour)
        surface.flush()
        data = surface.get_data()
        opaque = [index for index in range(0, len(data), 4) if data[index + 3] == 255]
        self.assertTrue(opaque)
        pixel = opaque[len(opaque) // 2]
        self.assertAlmostEqual(data[pixel + 2], 64, delta=1)
        self.assertAlmostEqual(data[pixel + 1], 191, delta=1)
        self.assertAlmostEqual(data[pixel], 128, delta=1)

    def test_desktop_file_icon_is_used_when_no_bundled_brand_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actual-app.png"
            rows = [[(0, 0, 0, 0)] * 6 for _ in range(6)]
            rows[2] = [(0, 0, 0, 0), (255, 255, 255, 255), (255, 255, 255, 255),
                       (255, 255, 255, 255), (255, 255, 255, 255), (0, 0, 0, 0)]
            pixbuf(rows).savev(str(path), "png", [], [])
            entry = AppEntry("new-app.desktop", "New App", icon=Gio.FileIcon.new(Gio.File.new_for_path(str(path))))
            mask = load_app_mask(entry, pixels=6)
            self.assertEqual((mask.get_width(), mask.get_height()), (4, 1))

    def test_missing_original_icon_returns_lettermark_without_category_substitution(self):
        theme = Mock()
        theme.lookup_icon.return_value = None
        entry = AppEntry("new-app.desktop", "New App")
        self.assertIsNone(load_app_mask(entry, theme=theme))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 30, 30)
        paint_app_icon(cairo.Context(surface), entry, 0, 0, 30, (1, 1, 1, 1), theme=theme)
        self.assertTrue(any(surface.get_data()))
        theme.lookup_icon.assert_called_with("new-app", 96, 16)

    def test_bundled_masks_are_cached_independently_of_colour(self):
        entry = AppEntry("google-chrome.desktop", "Chrome")
        self.assertIs(load_app_mask(entry), load_app_mask(entry))


if __name__ == "__main__":
    unittest.main()
