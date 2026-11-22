"""Theme-coloured UI glyphs and recognizable, application-specific logos."""
from collections import Counter
from functools import lru_cache
import math
from pathlib import Path
import re

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GdkPixbuf, Gtk


_ASSETS = Path(__file__).resolve().parent.parent / "assets/icons"
_APP_ASSETS = {
    "google-chrome.desktop": "googlechrome", "chromium.desktop": "googlechrome",
    "org.chromium.chromium.desktop": "googlechrome",
    "kitty.desktop": "kitty",
    "foot.desktop": "foot", "footclient.desktop": "foot", "foot-server.desktop": "foot",
    "thunar.desktop": "thunar", "thunar-settings.desktop": "thunar",
    "thunar-bulk-rename.desktop": "thunar",
    "discord.desktop": "discord", "com.discordapp.discord.desktop": "discord",
    "org.telegram.desktop.desktop": "telegram", "telegramdesktop.desktop": "telegram",
    "telegram-desktop.desktop": "telegram",
    "spotify.desktop": "spotify", "com.spotify.client.desktop": "spotify",
    "steam.desktop": "steam", "com.valvesoftware.steam.desktop": "steam",
    "counter-strike 2.desktop": "counterstrike", "steam_app_730.desktop": "counterstrike",
    "cs2.desktop": "counterstrike",
    "minecraft-tui.desktop": "minecraft", "minecraft.desktop": "minecraft",
    "minecraft-launcher.desktop": "minecraft", "com.mojang.minecraft.desktop": "minecraft",
    "chatgpt.desktop": "openai", "com.openai.chatgpt.desktop": "openai",
    "nvim.desktop": "neovim", "neovim.desktop": "neovim", "io.neovim.nvim.desktop": "neovim",
    "vim.desktop": "vim", "gvim.desktop": "vim",
    "cmake-gui.desktop": "cmake", "htop.desktop": "htop",
    "org.prismlauncher.prismlauncher.desktop": "prism",
    "org.openrgb.openrgb.desktop": "openrgb",
}


def application_asset(entry):
    """Select only explicit desktop IDs, never executable wrapper substrings."""
    if getattr(entry, "icon_override", ""):
        return None
    name = _APP_ASSETS.get(entry.id.casefold())
    if name is None and re.fullmatch(r"(?:jconsole|jshell)-java\d+-openjdk\.desktop", entry.id.casefold()):
        name = "java"
    path = _ASSETS / f"{name}.svg" if name else None
    return path if path is not None and path.is_file() else None


def _crop_mask(values, width, height):
    occupied = [(index % width, index // width) for index, value in enumerate(values) if value > 3]
    if not occupied:
        return None
    left, right = min(x for x, _ in occupied), max(x for x, _ in occupied)
    top, bottom = min(y for _, y in occupied), max(y for _, y in occupied)
    width_out, height_out = right - left + 1, bottom - top + 1
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_A8, width_out)
    data = bytearray(stride * height_out)
    for y in range(height_out):
        start = (y + top) * width + left
        data[y * stride:y * stride + width_out] = values[start:start + width_out]
    return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_A8, width_out, height_out, stride)


def monochrome_mask(pixbuf, *, alpha_only=False):
    """Preserve a logo's geometry while discarding its original brand colours.

    Symbolic/vector logos use their alpha directly. For opaque artwork, contrast
    against its dominant colour extracts interior details: a white icon tile does
    not become a solid square, and coloured backgrounds do not obscure the logo.
    """
    width, height = pixbuf.get_width(), pixbuf.get_height()
    channels, stride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    source = pixbuf.get_pixels()
    pixels = []
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * channels
            r, g, b = source[offset:offset + 3]
            alpha = source[offset + 3] if channels == 4 else 255
            pixels.append((r, g, b, alpha))
    visible = [pixel for pixel in pixels if pixel[3] > 190]
    if not visible:
        return _crop_mask(bytearray(pixel[3] for pixel in pixels), width, height)
    bins = Counter((r // 24, g // 24, b // 24) for r, g, b, _ in visible)
    if alpha_only:
        values = bytearray(pixel[3] for pixel in pixels)
    elif len(bins) == 1:
        # Uniform, opaque square artwork contains no recoverable logo geometry.
        corners = (pixels[0], pixels[width - 1], pixels[-width], pixels[-1])
        if all(pixel[3] > 240 for pixel in corners):
            return None
        values = bytearray(pixel[3] for pixel in pixels)
    else:
        dominant, count = bins.most_common(1)[0]
        background = [pixel for pixel in visible if tuple(channel // 24 for channel in pixel[:3]) == dominant]
        bg = tuple(sum(pixel[channel] for pixel in background) / len(background) for channel in range(3))
        contrasts = [max(abs(pixel[channel] - bg[channel]) for channel in range(3)) for pixel in pixels]
        peak = max(contrasts[index] for index, pixel in enumerate(pixels) if pixel[3] > 190) or 1
        opaque_tile = len(visible) / len(pixels) > 0.95
        if count / len(visible) >= 0.22:
            # Keep a faint original silhouette for transparent, multicolour art.
            # Opaque tiles instead lose their background entirely.
            baseline = 0 if opaque_tile else 0.12
            values = bytearray(round(pixel[3] * (baseline + (1 - baseline) * min(1, contrast / peak) ** 0.65))
                               for pixel, contrast in zip(pixels, contrasts))
        else:
            luminances = [(r * .2126 + g * .7152 + b * .0722) for r, g, b, _ in pixels]
            lo = min(value for value, pixel in zip(luminances, pixels) if pixel[3] > 190)
            hi = max(value for value, pixel in zip(luminances, pixels) if pixel[3] > 190)
            values = bytearray(round(pixel[3] * (.2 + .8 * max(0, min(1, (value - lo) / max(hi - lo, 1)))))
                               for pixel, value in zip(pixels, luminances))
    return _crop_mask(values, width, height)


@lru_cache(maxsize=160)
def _file_mask(path, pixels, symbolic):
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, pixels, pixels, True)
    return monochrome_mask(pixbuf, alpha_only=symbolic)


def load_app_mask(entry, pixels=96, theme=None):
    """Return a cached alpha surface for the real application icon, if present."""
    asset = application_asset(entry)
    if asset:
        return _file_mask(str(asset), pixels, True)
    try:
        if isinstance(entry.icon, Gio.FileIcon):
            path = entry.icon.get_file().get_path()
            if path:
                return _file_mask(path, pixels, False)
        theme = theme or Gtk.IconTheme.get_default()
        if theme is None:
            return None
        names = entry.icon.get_names() if isinstance(entry.icon, Gio.ThemedIcon) else []
        for name in names:
            symbolic_name = name if name.endswith("-symbolic") else name + "-symbolic"
            info = theme.lookup_icon(symbolic_name, pixels, Gtk.IconLookupFlags.FORCE_SIZE)
            if info and info.get_filename():
                return _file_mask(info.get_filename(), pixels, True)
        info = theme.lookup_by_gicon(entry.icon, pixels, Gtk.IconLookupFlags.FORCE_SIZE) if entry.icon else None
        if not info:
            info = theme.lookup_icon(entry.id.removesuffix(".desktop"), pixels, Gtk.IconLookupFlags.FORCE_SIZE)
        if info:
            path = info.get_filename()
            return _file_mask(path, pixels, "-symbolic" in Path(path).stem) if path else monochrome_mask(info.load_icon())
    except Exception:
        # A removed or malformed third-party icon cannot prevent launching apps.
        return None
    return None


def paint_app_icon(context, entry, x, y, size, color, *, mask=None, theme=None):
    """Paint a single theme colour, useful both to widgets and offline previews."""
    context.save()
    context.set_source_rgba(*color)
    mask = mask if mask is not None else load_app_mask(entry, theme=theme)
    if mask is not None:
        extent = max(1, size - 2)
        scale = extent / max(mask.get_width(), mask.get_height())
        context.translate(x + (size - mask.get_width() * scale) / 2,
                          y + (size - mask.get_height() * scale) / 2)
        context.scale(scale, scale)
        context.mask_surface(mask, 0, 0)
    else:
        # No generic category glyph: an absent original gets its own lettermark.
        words = entry.name.split()
        initials = (words[0][0] + words[1][0] if len(words) > 1 else entry.name[:2]).upper() if words else "?"
        context.select_font_face("Adwaita Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        context.set_font_size(size * .56)
        bearing_x, bearing_y, width, height, _, _ = context.text_extents(initials)
        context.move_to(x + (size - width) / 2 - bearing_x, y + (size - height) / 2 - bearing_y)
        context.show_text(initials)
    context.restore()


class AppIcon(Gtk.DrawingArea):
    """An application logo recoloured from the current `.app-icon` CSS colour."""
    def __init__(self, entry, size=23):
        super().__init__()
        self.entry = entry
        self.mask = load_app_mask(entry)
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def _draw(self, widget, context):
        allocation = self.get_allocation()
        size = min(allocation.width, allocation.height)
        color = self.get_style_context().get_color(self.get_state_flags())
        paint_app_icon(context, self.entry, (allocation.width - size) / 2,
                       (allocation.height - size) / 2, size,
                       (color.red, color.green, color.blue, color.alpha), mask=self.mask)
        return False


def app_icon(entry):
    key = (entry.id + " " + entry.name).lower()
    for words, icon in [
        (("chrome", "firefox", "chromium", "browser", "браузер"), "browser"),
        (("kitty", "foot", "terminal", "alacritty", "консоль"), "terminal"),
        (("thunar", "nautilus", "dolphin", "file", "файл"), "folder"),
        (("nm-connection", "settings", "control", "config", "настрой", "lact"), "sliders"),
        (("codex", "code", "nvim", "vim", "editor", "редактор"), "code"),
        (("discord", "telegram", "signal", "chat", "element"), "chat"),
        (("steam", "lutris", "game", "prism", "minecraft"), "game"),
        (("spotify", "music", "audio", "pavu", "pulse"), "music"),
        (("video", "vlc", "mpv"), "play"),
        (("image", "photo", "gimp", "inkscape", "изображ"), "image"),
        (("monitor", "htop", "system", "систем"), "activity"),
        (("pdf", "document", "office", "документ"), "document"),
    ]:
        if any(word in key for word in words):
            return icon
    return "app"


class Icon(Gtk.DrawingArea):
    def __init__(self, name="app", size=22):
        super().__init__()
        self.name = name
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def _draw(self, widget, c):
        a = self.get_allocation()
        size = min(a.width, a.height)
        c.translate((a.width - size) / 2, (a.height - size) / 2)
        c.scale(size / 24, size / 24)
        color = self.get_style_context().get_color(self.get_state_flags())
        c.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        c.set_line_width(1.55)
        c.set_line_cap(1)
        c.set_line_join(1)

        def line(*points):
            c.move_to(*points[0])
            for point in points[1:]: c.line_to(*point)
            c.stroke()

        def circle(x, y, r):
            c.new_sub_path(); c.arc(x, y, r, 0, math.tau); c.stroke()

        def rect(x, y, w, h, r=2):
            c.new_sub_path()
            for cx, cy, start in [(x+w-r, y+r, -math.pi/2), (x+w-r, y+h-r, 0), (x+r,y+h-r,math.pi/2), (x+r,y+r,math.pi)]:
                c.arc(cx,cy,r,start,start+math.pi/2)
            c.close_path(); c.stroke()

        n = self.name
        if n in ("apps", "app"):
            for x in (4, 14):
                for y in (4, 14): rect(x, y, 6, 6, 1.3)
        elif n in ("shell", "terminal"):
            if n == "terminal": rect(3,4,18,16,2.5)
            line((7,8),(11,12),(7,16)); line((13,16),(17,16))
        elif n == "ai":
            line((12,3),(14.5,9.5),(21,12),(14.5,14.5),(12,21),(9.5,14.5),(3,12),(9.5,9.5),(12,3))
        elif n == "search":
            circle(10,10,6); line((14.5,14.5),(20,20))
        elif n == "browser":
            circle(12,12,9); c.save(); c.translate(12,12); c.scale(.43,1); circle(0,0,9); c.restore(); line((3,12),(21,12))
        elif n == "folder":
            line((3,8),(3,5),(9,5),(11,8),(21,8),(20,20),(4,20),(3,8),(21,8))
        elif n == "code":
            line((8,6),(2,12),(8,18)); line((16,6),(22,12),(16,18)); line((14,4),(10,20))
        elif n == "chat":
            line((5,4),(19,4),(21,6),(21,16),(19,18),(10,18),(5,22),(5,18),(3,16),(3,6),(5,4)); line((8,10),(16,10)); line((8,14),(13,14))
        elif n == "game":
            line((7,7),(17,7),(20,9),(22,18),(20,20),(15,16),(9,16),(4,20),(2,18),(4,9),(7,7)); line((7,10),(7,14)); line((5,12),(9,12)); circle(16,11,.7); circle(18,13,.7)
        elif n == "music":
            line((9,18),(9,5),(20,3),(20,16)); circle(6,18,3); circle(17,16,3); line((9,9),(20,7))
        elif n == "play":
            rect(3,4,18,16,3); line((10,8),(16,12),(10,16),(10,8))
        elif n in ("settings", "sliders"):
            for x,y in [(5,8),(12,16),(19,10)]:
                line((x,3),(x,y-2)); line((x,y+2),(x,21)); circle(x,y,2)
        elif n == "image":
            rect(3,3,18,18); circle(8,8,1.5); line((3,17),(9,12),(13,16),(17,11),(21,15))
        elif n == "activity":
            line((2,12),(6,12),(9,4),(14,20),(17,12),(22,12))
        elif n == "document":
            line((6,3),(14,3),(20,9),(20,21),(4,21),(4,3),(6,3)); line((14,3),(14,9),(20,9)); line((8,13),(16,13)); line((8,17),(14,17))
        elif n == "enter":
            line((19,5),(19,14),(5,14)); line((10,9),(5,14),(10,19))
        elif n == "arrow":
            line((4,12),(20,12)); line((14,6),(20,12),(14,18))
        elif n == "back":
            line((20,12),(4,12)); line((10,6),(4,12),(10,18))
        elif n == "close":
            line((6,6),(18,18)); line((18,6),(6,18))
        elif n == "hide":
            line((3,3),(21,21)); c.arc(12,12,7,.2,2.7); c.stroke(); c.arc(12,12,7,3.4,5.8); c.stroke()
        elif n == "check":
            line((5,12),(10,17),(20,6))
        elif n == "pin":
            line((8,3),(16,3),(15,9),(18,12),(6,12),(9,9),(8,3)); line((12,12),(12,21))
        return False
