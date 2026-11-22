"""The entire interface is one small Wayland layer surface."""
from __future__ import annotations

from urllib.parse import urlsplit

import subprocess
import threading
import time
import copy
import re
import os

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkLayerShell, Pango

from .ai import AiClient, DEFAULT_MODEL
from .catalog import Catalog
from .config import Config, DEFAULTS, MODES, validate
from .icons import AppIcon, Icon, app_icon
from .shell import ShellHistory, executable_matches, run_command

NAMES = {"apps": "ПРИЛОЖЕНИЯ", "shell": "SHELL", "ai": "ПОМОЩНИК"}
PLACEHOLDERS = {"apps": "Найти приложение…", "shell": "Ввести команду…", "ai": "Спросить или сделать…"}
APP_KINDS = {
    "browser": "Браузер", "terminal": "Терминал", "folder": "Файлы",
    "code": "Разработка", "chat": "Общение", "game": "Игры",
    "music": "Музыка и звук", "play": "Видео", "sliders": "Настройки",
    "image": "Графика", "activity": "Система", "document": "Документы",
}


def label(text="", css=None, xalign=0):
    item = Gtk.Label(label=text, xalign=xalign)
    if css:
        item.get_style_context().add_class(css)
    return item


def box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, css=None):
    item = Gtk.Box(orientation=orientation, spacing=spacing)
    if css:
        item.get_style_context().add_class(css)
    return item


def button(icon, tooltip, callback, css="icon-button", size=19):
    item = Gtk.Button()
    item.set_relief(Gtk.ReliefStyle.NONE)
    item.set_can_focus(False)
    item.add(Icon(icon, size))
    item.set_tooltip_text(tooltip)
    item.get_style_context().add_class(css)
    item.connect("clicked", lambda *_: callback())
    return item


class Launcher:
    def __init__(self):
        self.config = Config()
        if not self.config.path.exists(): self.config.save()
        self.catalog = Catalog(self.config.data)
        self.history = ShellHistory()
        self.mode = self.enabled_modes()[0]
        self.visible = False
        self.view = "main"
        self.queries = dict.fromkeys(MODES, "")
        self.busy = False
        self.generation = 0
        self.cancel_event = threading.Event()
        self.client = AiClient(self.config.data)
        self.pending_command = None
        self.shell_workers = []
        self.results = []
        self.selected = 0
        self.css = Gtk.CssProvider()
        self.window = Gtk.Window(title="Лаунчер")
        self.window.set_name("luch")
        self.window.set_wmclass("luch", "Luch")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_app_paintable(True)
        self.window.set_visual(self.window.get_screen().get_rgba_visual())
        self.window.connect("delete-event", lambda *_: self.hide() or True)
        self.window.connect("key-press-event", self.key_pressed)
        self.window.connect("focus-out-event", self.focus_lost)
        if GtkLayerShell.is_supported():
            GtkLayerShell.init_for_window(self.window)
            GtkLayerShell.set_namespace(self.window, "luch")
            GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.EXCLUSIVE)
            GtkLayerShell.set_exclusive_zone(self.window, 0)
        else:
            self.window.set_position(Gtk.WindowPosition.CENTER)
            self.window.set_keep_above(True)
        Gtk.StyleContext.add_provider_for_screen(self.window.get_screen(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.frame = box(Gtk.Orientation.VERTICAL, css="frame")
        self.frame.set_name("panel")
        self.window.add(self.frame)
        self.build_header()
        self.frame.pack_start(Gtk.Separator(), False, False, 0)
        # A permanent viewport owns the body size. Individual modes can scroll,
        # but never change the layer surface's natural size or screen position.
        self.content_scroller = Gtk.ScrolledWindow()
        self.content_scroller.get_style_context().add_class("body-viewport")
        self.content_scroller.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.AUTOMATIC)
        self.content_scroller.set_propagate_natural_width(False)
        self.content_scroller.set_propagate_natural_height(False)
        self.content = box(Gtk.Orientation.VERTICAL, css="content")
        self.content_scroller.add(self.content)
        self.frame.pack_start(self.content_scroller, True, True, 0)
        self.frame.pack_start(Gtk.Separator(), False, False, 0)
        self.build_footer()
        self.apply_theme()
        self.sync_mode_buttons()
        self.render()
        # Desktop additions and removals show up without restarting the daemon.
        Gio.AppInfoMonitor.get().connect("changed", lambda *_: self.catalog.reload())

    def build_header(self):
        header = box(spacing=14, css="header")
        self.leading_icon = Icon("search", 21)
        self.leading_icon.get_style_context().add_class("muted")
        header.pack_start(self.leading_icon, False, False, 0)
        self.entry = Gtk.Entry()
        self.entry.set_has_frame(False)
        self.entry.set_hexpand(True)
        self.entry.set_width_chars(1)
        self.entry.set_max_width_chars(1)
        self.entry.connect("changed", self.query_changed)
        self.entry.connect("activate", lambda *_: self.activate())
        input_overlay = Gtk.Overlay()
        input_overlay.add(self.entry)
        self.placeholder = label(PLACEHOLDERS[self.mode], "placeholder")
        self.placeholder.set_halign(Gtk.Align.START)
        self.placeholder.set_valign(Gtk.Align.CENTER)
        self.placeholder.set_ellipsize(Pango.EllipsizeMode.END)
        input_overlay.add_overlay(self.placeholder)
        input_overlay.set_overlay_pass_through(self.placeholder, True)
        header.pack_start(input_overlay, True, True, 0)
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.get_style_context().add_class("mode-separator")
        header.pack_start(separator, False, False, 0)
        modes = box(spacing=4, css="modes")
        self.mode_buttons = {}
        for index, mode in enumerate(MODES):
            item = button(mode, f"{NAMES[mode].capitalize()} · Alt+{index+1}", lambda m=mode: self.set_mode(m), "mode-button", 20)
            modes.pack_start(item, False, False, 0)
            self.mode_buttons[mode] = item
        header.pack_end(modes, False, False, 0)
        self.frame.pack_start(header, False, False, 0)

    def build_footer(self):
        footer = box(spacing=12, css="footer")
        self.hints = label("↑↓  выбрать    ↵  открыть", "hint")
        self.hints.set_hexpand(True)
        self.hints.set_ellipsize(Pango.EllipsizeMode.END)
        self.hints.set_width_chars(1)
        self.hints.set_max_width_chars(1)
        footer.pack_start(self.hints, True, True, 0)
        self.save_button = Gtk.Button(label="Сохранить")
        self.save_button.set_can_focus(False)
        self.save_button.set_no_show_all(True)
        self.save_button.get_style_context().add_class("editor-save")
        self.save_button.connect("clicked", lambda *_: self.save_app())
        footer.pack_start(self.save_button, False, False, 0)
        self.mode_hint = label(css="hint")
        footer.pack_start(self.mode_hint, False, False, 0)
        footer.pack_start(button("settings", "Настройки · Ctrl+,", self.settings, size=17), False, False, 0)
        self.frame.pack_start(footer, False, False, 0)

    def apply_theme(self):
        w, t = self.config.data["window"], self.config.data["theme"]
        rgb = tuple(int(t["background"][i:i+2], 16) for i in (1,3,5))
        css = f'''
        #luch {{ background: transparent; color: {t['text']}; font-family: "{w['font']}"; font-size: {w['font_size']}px; }}
        #luch #panel {{ background: rgba({rgb[0]},{rgb[1]},{rgb[2]},{w['opacity']}); border: 1px solid {t['border']}; border-radius: {w['radius']}px; }}
        #luch label {{ color: {t['text']}; }}
        #luch .header {{ padding: 17px {w['padding']}px; }}
        #luch entry {{ color: {t['text']}; background: transparent; padding: 7px 0; border: none; box-shadow: none; caret-color: {t['accent']}; font-size: {w['font_size']+3}px; min-height: 24px; }}
        #luch entry selection {{ background: {t['selection']}; color: {t['text']}; }}
        #luch .placeholder {{ color: {t['muted']}; font-size: {w['font_size']+3}px; }}
        #luch separator {{ background: {t['border']}; min-height: 1px; opacity: 0.4; }}
        #luch .mode-separator {{ margin: 8px 1px; min-width: 1px; }}
        #luch button {{ color: {t['muted']}; background: transparent; border: none; box-shadow: none; text-shadow: none; padding: 7px; border-radius: 9px; min-height: 0; min-width: 0; }}
        #luch button:hover {{ color: {t['accent']}; background: {t['surface']}; }}
        #luch .mode-button {{ padding: 9px; }}
        #luch .mode-button.active {{ color: mix({t['accent']}, {t['text']}, 0.45); background: {t['selection']}; }}
        #luch .body-viewport {{ padding: 12px 0; }}
        #luch .content {{ padding: 0 {w['padding']-8}px; }}
        #luch .section {{ color: {t['muted']}; font-size: 10px; letter-spacing: 1.6px; padding: 0 12px 8px; }}
        #luch .count {{ font-family: "JetBrainsMono Nerd Font"; font-size: 10px; color: {t['muted']}; padding-right: 12px; }}
        #luch list {{ background: transparent; }}
        #luch row {{ border-radius: 10px; padding: 0; background: transparent; outline: none; border: none; }}
        #luch row:selected {{ background: {t['selection']}; }}
        #luch row:hover {{ background: {t['surface']}; }}
        #luch row:selected:hover {{ background: {t['selection']}; }}
        #luch .result {{ padding: 0 12px; min-height: {w['row_height']}px; }}
        #luch .app-icon {{ color: {t['muted']}; }}
        #luch row:selected .app-icon {{ color: mix({t['muted']}, {t['text']}, 0.5); }}
        #luch .app-title {{ color: {t['text']}; font-size: {w['font_size']}px; }}
        #luch .app-detail {{ color: {t['muted']}; font-size: 11px; }}
        #luch .row-action {{ color: {t['muted']}; font-size: 11px; }}
        #luch .footer {{ padding: 8px {w['padding']}px; }}
        #luch .hint {{ color: {t['muted']}; font-size: 10px; }}
        #luch .accent {{ color: {t['accent']}; }}
        #luch .muted {{ color: {t['muted']}; }}
        #luch .success {{ color: {t['success']}; }}
        #luch .error {{ color: {t['error']}; }}
        #luch .welcome {{ padding: 20px 24px; }}
        #luch .welcome-title {{ font-size: 21px; padding-top: 16px; padding-bottom: 8px; }}
        #luch .subtitle {{ color: {t['muted']}; font-size: 12px; }}
        #luch .suggestion {{ padding: 10px 13px; background: {t['surface']}; color: {t['text']}; font-size: 12px; border: 1px solid alpha({t['border']},0.4); }}
        #luch .suggestion:hover {{ background: {t['selection']}; color: {t['accent']}; }}
        #luch .answer {{ padding: 12px; }}
        #luch .answer-text {{ font-size: {w['font_size']+1}px; }}
        #luch .command {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; padding: 14px; margin-top: 12px; }}
        #luch .mono {{ font-family: "JetBrainsMono Nerd Font"; font-size: 12px; }}
        #luch .primary {{ background: {t['accent']}; color: {t['background']}; padding: 8px 14px; font-weight: 600; }}
        #luch .primary:hover {{ background: shade({t['accent']},1.08); }}
        #luch .primary label {{ color: {t['background']}; }}
        #luch .settings {{ padding: 10px 12px; }}
        #luch .app-config {{ opacity: 0; padding: 6px; }}
        #luch row:selected .app-config, #luch row:hover .app-config {{ opacity: 1; }}
        #luch .editor-save {{ background: {t['accent']}; padding: 6px 10px; }}
        #luch .editor-save label {{ color: {t['background']}; font-size: 11px; font-weight: 600; }}
        #luch .app-editor {{ padding: 4px 12px 10px; }}
        #luch .editor-row {{ padding: 5px 0; }}
        #luch .editor-heading {{ padding: 0 0 10px; }}
        #luch entry.preference-entry {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 10px; font-size: {w['font_size']}px; }}
        #luch entry.preference-entry:focus {{ border-color: {t['accent']}; }}
        #luch .editor-toggle {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px; }}
        #luch .editor-toggle:checked {{ background: {t['accent']}; }}
        #luch .editor-toggle slider {{ background: {t['text']}; border-radius: 10px; }}
        #luch combobox button {{ background: {t['surface']}; border: 1px solid {t['border']}; padding: 7px 10px; }}
        #luch expander {{ padding-top: 8px; }}
        #luch .setting {{ padding: 9px 0; }}
        #luch scale trough {{ background: {t['surface']}; min-height: 3px; border: none; }}
        #luch scale highlight {{ background: {t['accent']}; border: none; }}
        #luch scale slider {{ background: {t['accent']}; min-width: 12px; min-height: 12px; border: none; box-shadow: none; }}
        #luch scrollbar {{ background: transparent; min-width: 4px; }}
        #luch scrollbar slider {{ background: {t['border']}; min-width: 4px; border-radius: 4px; }}
        #luch scrolledwindow, #luch viewport {{ border: none; border-radius: 0; background: transparent; box-shadow: none; }}
        '''
        self.css.load_from_data(css.encode())
        self.frame.set_size_request(w["width"], -1)
        body_height = w["rows"] * w["row_height"] + 21
        # Reset the previous bounds before adjusting rows in the settings view.
        self.content_scroller.set_min_content_height(-1)
        self.content_scroller.set_max_content_height(-1)
        self.content_scroller.set_min_content_height(body_height)
        self.content_scroller.set_max_content_height(body_height)
        self.window.resize(w["width"], 1)
        if GtkLayerShell.is_layer_window(self.window):
            for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM, GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                GtkLayerShell.set_anchor(self.window, edge, False)
                GtkLayerShell.set_margin(self.window, edge, 0)
            position = w["position"]
            if position != "center":
                edge = GtkLayerShell.Edge.TOP if position == "top" else GtkLayerShell.Edge.BOTTOM
                GtkLayerShell.set_anchor(self.window, edge, True)
                GtkLayerShell.set_margin(self.window, edge, w["top_margin"])

    def choose_monitor(self):
        if not GtkLayerShell.is_layer_window(self.window): return
        display = Gdk.Display.get_default()
        monitor = None
        requested = self.config.data["window"]["monitor"]
        # Sway exposes the focused output. Avoid stealing the pointer to find it.
        try:
            import json
            output = json.loads(subprocess.check_output(["swaymsg", "-t", "get_outputs", "-r"], timeout=.3))
            target = next((o for o in output if o.get("name") == requested), None) if requested else next((o for o in output if o.get("focused")), None)
            if target:
                monitor = next((display.get_monitor(i) for i in range(display.get_n_monitors()) if display.get_monitor(i).get_geometry().x == target["rect"]["x"] and display.get_monitor(i).get_geometry().y == target["rect"]["y"]), None)
        except Exception:
            pass
        if monitor is None:
            pointer = display.get_default_seat().get_pointer()
            screen, x, y = pointer.get_position()
            monitor = display.get_monitor_at_point(x, y)
        GtkLayerShell.set_monitor(self.window, monitor)

    def enabled_modes(self):
        return tuple(mode for mode in MODES if self.config.data["modes"][mode])

    def sync_mode_buttons(self):
        enabled = self.enabled_modes()
        for mode, item in self.mode_buttons.items():
            item.set_no_show_all(mode not in enabled)
            item.set_visible(mode in enabled)
            context = item.get_style_context()
            (context.add_class if mode == self.mode else context.remove_class)("active")
        if self.view == "main":
            self.mode_hint.set_text("tab  режим" if len(enabled) > 1 else "")

    def cycle_mode(self, step=1, *, close_on_last=False):
        enabled = self.enabled_modes()
        index = enabled.index(self.mode) + step
        if close_on_last and index == len(enabled):
            self.hide()
            return
        mode = enabled[index % len(enabled)]
        if mode != self.mode:
            self.set_mode(mode)

    def dispatch(self, command):
        if command == "toggle":
            if self.visible:
                self.cycle_mode(close_on_last=self.config.data["modes"]["on_last_toggle"] == "close")
            else:
                self.show()
        elif command in MODES:
            if not self.visible: self.show(command)
            else: self.set_mode(command)
        elif command == "hide": self.hide()
        elif command == "reload":
            self.config.reload()
            self.catalog = Catalog(self.config.data)
            self.apply_theme()
            self.set_mode(self.mode)
            self.client = AiClient(self.config.data)
        elif command == "settings":
            if not self.visible: self.show()
            self.settings()
        elif command == "quit":
            self.hide()
            # Shell cancellation owns a process group. Let its cleanup finish
            # before the interpreter exits, while launched GUI apps stay alive.
            deadline = time.monotonic() + 1.2
            for worker in getattr(self, "shell_workers", []):
                worker.join(timeout=max(0, deadline-time.monotonic()))
            Gtk.main_quit()

    def status(self):
        return {"visible": self.visible, "mode": self.mode, "view": self.view, "busy": self.busy, "query": self.entry.get_text(), "results": len(self.results), "selected": self.selected, "pending_command": self.pending_command, "config_errors": self.config.errors}

    def show(self, mode=None):
        self.config.reload()
        self.catalog = Catalog(self.config.data)
        self.client = AiClient(self.config.data)
        self.apply_theme()
        self.choose_monitor()
        self.entry.set_text("")
        self.queries = dict.fromkeys(MODES, "")
        self.visible = True
        self.set_mode(mode)
        self.window.show_all()
        self.placeholder.set_visible(not self.entry.get_text())
        self.window.present()
        self.entry.grab_focus()
        if self.config.errors:
            self.hints.set_text("Ошибка config.toml · использованы корректные настройки")

    def hide(self):
        self.stop_work()
        self.visible = False
        self.window.hide()
        return False

    def stop_work(self):
        self.generation += 1
        self.cancel_event.set()
        self.client.cancel()
        self.busy = False
        self.pending_command = None

    def set_mode(self, mode):
        enabled = self.enabled_modes()
        if mode not in enabled:
            mode = enabled[0]
        self.queries[self.mode] = self.entry.get_text()
        self.stop_work()
        self.mode, self.view = mode, "main"
        self.entry.set_sensitive(True)
        self.save_button.hide()
        self.placeholder.set_text(PLACEHOLDERS[mode])
        self.leading_icon.name = "search" if mode == "apps" else mode
        self.leading_icon.queue_draw()
        self.entry.set_text(self.queries[mode])
        self.entry.set_position(-1)
        self.sync_mode_buttons()
        self.render()
        self.entry.grab_focus()

    def query_changed(self, *_):
        self.placeholder.set_visible(not self.entry.get_text())
        if self.view in ("settings", "ai_settings", "hidden", "app_manager", "app_edit"):
            if self.view == "hidden": self.render_hidden()
            elif self.view == "app_manager": self.app_manager_view()
            return
        if self.mode != "ai":
            if self.busy: self.stop_work()
            self.render()

    def clear_content(self):
        for child in self.content.get_children(): child.destroy()
        self.results = []
        self.listbox = None
        self.list_scroller = None

    def section(self, title, detail=""):
        row = box()
        heading = label(title, "section")
        heading.set_ellipsize(Pango.EllipsizeMode.END)
        row.pack_start(heading, True, True, 0)
        row.pack_end(label(detail, "count"), False, False, 0)
        self.content.pack_start(row, False, False, 0)

    def render(self):
        self.clear_content()
        if self.mode == "apps": self.render_apps()
        elif self.mode == "shell": self.render_shell()
        else: self.render_ai_welcome()
        self.content.show_all()
        self.content_scroller.get_vadjustment().set_value(0)

    def make_list(self, scroll=False):
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(True)
        self.listbox.connect("row-activated", lambda _, row: self.activate(row.get_index()))
        self.listbox.connect("row-selected", self.row_selected)
        self.list_scroller = self.content_scroller
        self.content.pack_start(self.listbox, False, False, 0)

    def row_selected(self, _, row):
        if row is not None: self.selected = row.get_index()

    def add_row(self, title, subtitle, icon_name, action="", entry=None):
        row = Gtk.ListBoxRow()
        row.set_can_focus(False)
        inner = box(spacing=15, css="result")
        image = AppIcon(entry, 23) if entry else Icon(icon_name, 23)
        image.get_style_context().add_class("app-icon")
        inner.pack_start(image, False, False, 0)
        texts = box(Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        name = label(title, "app-title")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(44)
        texts.pack_start(name, False, False, 0)
        if subtitle:
            desc = label(subtitle, "app-detail")
            desc.set_ellipsize(Pango.EllipsizeMode.END)
            desc.set_max_width_chars(55)
            texts.pack_start(desc, False, False, 0)
        inner.pack_start(texts, True, True, 0)
        if action:
            action_label = label(action, "row-action")
            inner.pack_end(action_label, False, False, 0)
        if entry:
            inner.pack_end(button("settings", "Настроить приложение · Ctrl+E", lambda: self.edit_app(entry), "app-config", 16), False, False, 0)
            if entry.pinned:
                mark = Icon("pin", 14)
                mark.get_style_context().add_class("muted")
                mark.set_tooltip_text("Закреплено")
                inner.pack_end(mark, False, False, 0)
            def context_menu(_, event):
                if event.button == 3:
                    self.edit_app(entry)
                    return True
                return False
            row.connect("button-press-event", context_menu)
        row.add(inner)
        self.listbox.add(row)

    def finish_list(self):
        first = self.listbox.get_row_at_index(0)
        if first: self.listbox.select_row(first)

    def render_apps(self):
        query = self.entry.get_text()
        self.results = self.catalog.search(query, limit=max(len(self.catalog.all()), self.config.data["window"]["rows"]))
        self.section("РЕЗУЛЬТАТЫ" if query else "ПРИЛОЖЕНИЯ", f"{len(self.results)}")
        self.make_list(scroll=True)
        for index, entry in enumerate(self.results):
            icon_name = app_icon(entry)
            self.add_row(entry.name, entry.subtitle or APP_KINDS.get(icon_name, "Приложение"), icon_name, entry=entry)
        if not self.results:
            self.content.pack_start(label("Ничего не найдено", "muted", .5), False, False, 28)
        self.finish_list()
        self.hints.set_text("↑↓  выбрать    ↵  открыть    ctrl+e  настроить")

    def render_shell(self):
        query = self.entry.get_text().strip()
        self.section("КОМАНДА" if query else "SHELL", "~")
        self.results = []
        if query: self.results.append(query)
        for item in self.history.items(query, limit=5):
            if item not in self.results: self.results.append(item)
        if query and " " not in query:
            for item in executable_matches(query, limit=5):
                if item not in self.results: self.results.append(item)
        self.results = self.results[:self.config.data["window"]["rows"]]
        self.make_list()
        for index, command in enumerate(self.results):
            self.add_row(command, "Выполнить в shell" if index == 0 and query else "История команд", "terminal")
        if not self.results:
            welcome = self.welcome("terminal", "Команда — и готово.", "Вывод появится здесь. Shift+Enter откроет терминал.")
            self.content.pack_start(welcome, True, True, 0)
        else: self.finish_list()
        self.hints.set_text("↵  выполнить    shift+↵  в терминале")

    def welcome(self, icon, title, subtitle):
        content = box(Gtk.Orientation.VERTICAL, css="welcome")
        content.set_valign(Gtk.Align.CENTER)
        image = Icon(icon, 34)
        image.get_style_context().add_class("accent")
        content.pack_start(image, False, False, 12)
        title_label = label(title, "welcome-title", .5)
        title_label.set_line_wrap(True)
        title_label.set_max_width_chars(30)
        title_label.set_justify(Gtk.Justification.CENTER)
        content.pack_start(title_label, False, False, 0)
        sub = label(subtitle, "subtitle", .5)
        sub.set_line_wrap(True)
        sub.set_justify(Gtk.Justification.CENTER)
        content.pack_start(sub, False, False, 0)
        return content

    def render_ai_welcome(self):
        self.section("ПОМОЩНИК", self.config.data["ai"]["model"])
        welcome = self.welcome("ai", "Спроси. Или попроси сделать.", "Короткий ответ и команды для твоего компьютера.")
        for text in ("Сколько свободно места на диске?", "Убавь громкость до 30%"):
            item = Gtk.Button(label=text)
            item.get_style_context().add_class("suggestion")
            item.set_can_focus(False)
            item.set_halign(Gtk.Align.CENTER)
            item.set_margin_top(10)
            item.connect("clicked", lambda _, q=text: self.ask(q))
            welcome.pack_start(item, False, False, 0)
        self.content.pack_start(welcome, True, True, 0)
        configure = Gtk.Button(label="Подключение к ИИ…")
        configure.set_halign(Gtk.Align.CENTER)
        configure.connect("clicked", lambda *_: self.ai_settings())
        welcome.pack_start(configure, False, False, 10)
        self.hints.set_text("↵  спросить    ctrl+,  настройки")

    def start_work(self):
        self.stop_work()
        self.cancel_event = threading.Event()
        self.busy = True
        return self.generation, self.cancel_event

    def post(self, generation, callback, *args):
        def run():
            if self.generation == generation and self.visible:
                callback(*args)
            return False
        GLib.idle_add(run)

    def shell_thread(self, target):
        self.shell_workers = [worker for worker in getattr(self, "shell_workers", []) if worker.is_alive()]
        worker = threading.Thread(target=target, daemon=True)
        self.shell_workers.append(worker)
        worker.start()

    def answer_view(self, prompt, title="ОТВЕТ"):
        self.clear_content()
        self.view = "answer"
        self.section(title)
        self.answer_body = box(Gtk.Orientation.VERTICAL, spacing=10, css="answer")
        self.content.pack_start(self.answer_body, False, False, 0)
        self.append_answer(prompt)
        self.content_scroller.get_vadjustment().set_value(0)

    def append_answer(self, prompt=None, scroll=False):
        if prompt is not None:
            question = label(prompt, "subtitle")
            question.set_line_wrap(True)
            question.set_max_width_chars(60)
            question.set_selectable(True)
            self.answer_body.pack_start(question, False, False, 0)
        self.answer_label = label("", "answer-text")
        self.answer_label.set_line_wrap(True)
        self.answer_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.answer_label.set_max_width_chars(60)
        self.answer_label.set_selectable(True)
        if scroll:
            generation = self.generation
            def allocated(widget, _allocation):
                widget.disconnect(handler)
                self.post(generation, self.scroll_answer)
            handler = self.answer_label.connect("size-allocate", allocated)
        self.answer_body.pack_start(self.answer_label, False, False, 0)
        self.command_area = box(Gtk.Orientation.VERTICAL)
        self.answer_body.pack_start(self.command_area, False, False, 0)
        self.content.show_all()
        self.answer_text = ""

    def scroll_answer(self):
        adjustment = self.content_scroller.get_vadjustment()
        adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))

    def ask(self, prompt):
        if not prompt.strip(): return
        if self.pending_command: self.deny_command()
        generation, cancel = self.start_work()
        if self.view == "answer":
            self.append_answer(prompt, scroll=True)
        else:
            self.answer_view(prompt)
        self.entry.set_text("")
        self.placeholder.set_text("Спросить ещё…")
        self.hints.set_text("Отвечаю…    esc  закрыть")
        client = self.client
        def worker():
            client.stream(prompt, lambda event: self.post(generation, self.ai_event, event), cancel)
        threading.Thread(target=worker, daemon=True).start()

    def ai_event(self, event):
        kind = event["type"]
        if kind == "text":
            self.answer_text += event["text"]
            self.answer_label.set_text(self.answer_text)
        elif kind == "command":
            self.pending_command = event["command"]
            self.show_command(event["command"], event.get("explanation", "Предлагаю выполнить"))
        elif kind == "done":
            self.busy = False
            if self.pending_command:
                self.hints.set_text("↵  выполнить команду    ctrl+backspace  отклонить")
            else:
                stats = event.get("stats", {})
                duration = stats.get("duration_seconds", 0)
                self.hints.set_text(f"{duration:.2f} с    ↵  продолжить диалог")
        elif kind == "error":
            self.busy = False
            self.answer_label.set_text(event["message"])
            self.hints.set_text("Проверь ИИ: luch doctor")

    def show_command(self, command, explanation):
        for child in self.command_area.get_children(): child.destroy()
        card = box(Gtk.Orientation.VERTICAL, spacing=10, css="command")
        reason = label(explanation, "subtitle")
        reason.set_line_wrap(True)
        card.pack_start(reason, False, False, 0)
        code = label(command, "mono")
        code.set_selectable(True)
        code.set_line_wrap(True)
        code.set_line_wrap_mode(Pango.WrapMode.CHAR)
        code.set_max_width_chars(65)
        card.pack_start(code, False, False, 0)
        actions = box(spacing=8)
        confirm = Gtk.Button(label="Выполнить  ↵")
        confirm.get_style_context().add_class("primary")
        confirm.set_can_focus(False)
        confirm.connect("clicked", lambda *_: self.approve_command())
        actions.pack_start(confirm, False, False, 0)
        deny = Gtk.Button(label="Отклонить")
        deny.set_can_focus(False)
        deny.connect("clicked", lambda *_: self.deny_command())
        actions.pack_start(deny, False, False, 0)
        card.pack_start(actions, False, False, 0)
        self.command_area.pack_start(card, False, False, 0)
        self.command_area.show_all()

    def deny_command(self):
        command = self.pending_command
        self.generation += 1
        self.cancel_event.set()
        self.busy = False
        self.pending_command = None
        self.client.cancel()
        for child in self.command_area.get_children(): child.destroy()
        status = label(f"$ {command}\nКоманда отклонена", "mono")
        status.set_line_wrap(True)
        status.set_line_wrap_mode(Pango.WrapMode.CHAR)
        status.set_max_width_chars(65)
        status.set_selectable(True)
        self.command_area.pack_start(status, False, False, 0)
        self.command_area.show_all()
        self.hints.set_text("Введи сообщение, чтобы продолжить диалог")
        self.entry.grab_focus()

    def approve_command(self):
        command = self.pending_command
        if not command or self.busy: return
        self.pending_command = None
        self.busy = True
        generation, cancel, client = self.generation, self.cancel_event, self.client
        for child in self.command_area.get_children(): child.destroy()
        status = label("$ " + command, "mono")
        status.set_line_wrap(True)
        status.set_max_width_chars(65)
        self.command_area.pack_start(status, False, False, 10)
        self.command_area.show_all()
        self.hints.set_text("Выполняю…    esc  остановить и закрыть")
        def worker():
            result = run_command(command, cwd=self.config.data["shell"]["cwd"], timeout=self.config.data["shell"]["timeout"], cancel=cancel, max_output=12000)
            output = f"Exit code: {result.returncode}\n{result.output}"
            if result.timed_out: output += "\nКоманда остановлена по таймауту."
            if cancel.is_set(): return
            self.post(generation, self.command_result, result)
            client.continue_after_command(command, output, lambda event: self.post(generation, self.ai_event, event), cancel)
        self.shell_thread(worker)

    def command_result(self, result):
        raw = label(result.output[:1800] or f"Код завершения: {result.returncode}", "mono")
        raw.set_line_wrap(True)
        raw.set_max_width_chars(65)
        raw.set_selectable(True)
        self.command_area.pack_start(raw, False, False, 0)
        self.command_area.show_all()
        self.append_answer(scroll=True)

    def execute_shell(self, command, terminal=False):
        if not command.strip(): return
        self.last_shell_command = command
        self.history.add(command)
        if terminal:
            import shlex
            try:
                process = subprocess.Popen(shlex.split(self.config.data["shell"]["terminal"]) + ["-e", os.environ.get("SHELL", "/bin/bash"), "-c", command + '; printf "\\nНажми Enter, чтобы закрыть…"; read -r _'], cwd=os.path.expanduser(self.config.data["shell"]["cwd"]), start_new_session=True)
                threading.Thread(target=process.wait, daemon=True).start()
                self.hide()
            except OSError as error:
                self.hints.set_text(str(error))
            return
        generation, cancel = self.start_work()
        self.answer_view("$ " + command, "SHELL")
        self.answer_label.get_style_context().add_class("mono")
        self.hints.set_text("Выполняю…    esc  остановить и закрыть")
        def worker():
            result = run_command(command, cwd=self.config.data["shell"]["cwd"], timeout=self.config.data["shell"]["timeout"], cancel=cancel)
            self.post(generation, self.shell_done, result)
        self.shell_thread(worker)

    def shell_done(self, result):
        self.busy = False
        self.answer_label.set_text(result.output or "Готово.")
        status = "Таймаут" if result.timed_out else f"Код {result.returncode}"
        self.hints.set_text(f"{status}    ↵  повторить    введи новую команду")

    def activate(self, index=None, terminal=False):
        if self.view in ("settings", "ai_settings"): return
        if self.view == "app_edit": return
        if self.view == "app_manager":
            idx = self.selected if index is None else index
            if self.results and idx < len(self.results): self.edit_app(self.results[idx])
            return
        if self.view == "hidden":
            idx = self.selected if index is None else index
            if self.results and idx < len(self.results):
                self.config.unhide(self.results[idx].id)
                self.catalog = Catalog(self.config.data)
                self.render_hidden()
            return
        if self.mode == "ai":
            if self.pending_command and not self.entry.get_text().strip(): self.approve_command()
            elif self.entry.get_text().strip(): self.ask(self.entry.get_text())
            return
        idx = self.selected if index is None else index
        if self.mode == "shell":
            command = self.results[idx] if self.results and idx < len(self.results) else self.entry.get_text() or (getattr(self, "last_shell_command", "") if self.view == "answer" else "")
            self.execute_shell(command, terminal)
        elif self.results and idx < len(self.results):
            try:
                context = Gdk.Display.get_default().get_app_launch_context()
                self.catalog.launch(self.results[idx], context)
                self.hide()
            except Exception as error:
                self.hints.set_text(f"Не удалось открыть: {error}")

    def move_selection(self, delta):
        if not self.listbox or not self.results: return
        self.selected = (self.selected + delta) % len(self.results)
        row = self.listbox.get_row_at_index(self.selected)
        self.listbox.select_row(row)
        if self.list_scroller:
            adjustment = self.list_scroller.get_vadjustment()
            rect = row.get_allocation()
            coordinates = row.translate_coordinates(self.content, 0, 0)
            row_y = coordinates[1] if coordinates else rect.y
            top, page = adjustment.get_value(), adjustment.get_page_size()
            if row_y < top: adjustment.set_value(row_y)
            elif row_y + rect.height > top + page:
                adjustment.set_value(row_y + rect.height - page)

    def hide_selected(self):
        if (self.mode != "apps" and self.view != "app_manager") or not self.results: return
        entry = self.results[self.selected]
        self.config.hide(entry.id)
        self.catalog = Catalog(self.config.data)
        if self.view == "app_manager": self.app_manager_view()
        else: self.render()
        self.hints.set_text(f"{entry.name} скрыто · ctrl+shift+h  вернуть")

    def render_hidden(self):
        self.clear_content()
        self.view = "hidden"
        self.placeholder.set_text("Найти скрытое приложение…")
        self.section("СКРЫТЫЕ ПРИЛОЖЕНИЯ", "↵  вернуть")
        hidden = self.catalog.hidden
        query = self.entry.get_text().casefold()
        self.results = [a for a in self.catalog.all(include_hidden=True) if a.id in hidden and query in (a.name+" "+a.id).casefold()]
        # Keep the manager scrollable even for a large hidden catalogue.
        self.make_list(scroll=True)
        for entry in self.results:
            self.add_row(entry.name, entry.id, app_icon(entry), "вернуть", entry)
        if not self.results:
            self.content.pack_start(label("Скрытых приложений нет", "muted", .5), False, False, 40)
        self.finish_list()
        self.hints.set_text("↵  вернуть приложение    tab  назад")
        self.content.show_all()

    def app_manager(self):
        self.set_mode("apps" if "apps" in self.enabled_modes() else self.mode)
        self.view = "app_manager"
        self.placeholder.set_text("Найти приложение, включая скрытые…")
        self.entry.set_text("")
        self.app_manager_view()
        self.entry.grab_focus()

    def app_manager_view(self):
        self.clear_content()
        self.view = "app_manager"
        self.placeholder.set_text("Найти приложение, включая скрытые…")
        self.section("НАСТРОЙКИ ПРИЛОЖЕНИЙ", str(len(self.catalog.all(include_hidden=True))))
        query = self.entry.get_text()
        self.results = self.catalog.search(query, hidden=[], limit=len(self.catalog.all(include_hidden=True))) if query.strip() else self.catalog.all(include_hidden=True)
        self.make_list(scroll=True)
        for entry in self.results:
            states = []
            if entry.id in self.catalog.hidden: states.append("скрыто")
            if entry.pinned: states.append("закреплено")
            detail = ", ".join(entry.aliases) if entry.aliases else entry.subtitle or APP_KINDS.get(app_icon(entry), "Приложение")
            self.add_row(entry.name, detail, app_icon(entry), " · ".join(states), entry)
        if not self.results:
            self.content.pack_start(label("Ничего не найдено", "muted", .5), False, False, 28)
        self.finish_list()
        self.hints.set_text("↵  настроить    alt+←  назад")
        self.content.show_all()

    def edit_app(self, entry=None):
        if entry is None:
            if self.mode != "apps" or not self.results: return
            entry = self.results[self.selected]
        if self.view != "app_edit":
            self.editor_return_view = self.view if self.view in ("main", "hidden", "app_manager") else "app_manager"
            self.editor_query = self.entry.get_text()
        self.stop_work()
        self.clear_content()
        self.view = "app_edit"
        self.editing_id = entry.id
        self.entry.set_text("")
        self.entry.set_sensitive(False)
        self.placeholder.set_text("Настройки приложения")
        self.mode_hint.set_text("tab  поля")
        self.save_button.show()
        self.section("ПРИЛОЖЕНИЕ", "ctrl+s  сохранить")
        form = box(Gtk.Orientation.VERTICAL, css="app-editor")
        heading = box(spacing=12, css="editor-heading")
        heading.pack_start(button("back", "Назад · Alt+←", self.editor_back, size=17), False, False, 0)
        image = AppIcon(entry, 28)
        image.get_style_context().add_class("app-icon")
        heading.pack_start(image, False, False, 0)
        title = label(entry.original_name or entry.name, "app-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        heading.pack_start(title, True, True, 0)
        reset = Gtk.Button(label="Сбросить")
        reset.set_tooltip_text("Вернуть исходное название, значок и запуск")
        reset.connect("clicked", lambda *_: self.reset_app())
        heading.pack_end(reset, False, False, 0)
        form.pack_start(heading, False, False, 0)
        values = self.config.app_settings(entry.id)
        self.editor_fields = {}

        def text_field(parent, key, title, placeholder, length=4096, tooltip=None):
            row = box(spacing=14, css="editor-row")
            caption = label(title, "subtitle")
            caption.set_size_request(100, -1)
            row.pack_start(caption, False, False, 0)
            field = Gtk.Entry()
            field.get_style_context().add_class("preference-entry")
            field.set_width_chars(1)
            field.set_max_width_chars(1)
            field.set_max_length(length)
            field.set_hexpand(True)
            field.set_placeholder_text(placeholder)
            value = ", ".join(values[key]) if key == "aliases" else values[key]
            if key == "name" and not value: value = entry.original_name or entry.name
            field.set_text(value)
            if tooltip: field.set_tooltip_text(tooltip)
            field.connect("activate", lambda *_: self.save_app())
            row.pack_start(field, True, True, 0)
            parent.pack_start(row, False, False, 0)
            self.editor_fields[key] = field

        text_field(form, "name", "Название", entry.original_name or entry.name, 200)
        text_field(form, "aliases", "Алиасы", "Через запятую: web, браузер, работа", 4096,
                   "Дополнительные слова для поиска; исходное название тоже остаётся доступно")
        for key, title in (("pinned", "Закрепить сверху"), ("hidden", "Скрыть из поиска")):
            row = box(spacing=14, css="editor-row")
            row.pack_start(label(title, "subtitle"), True, True, 0)
            switch = Gtk.Switch()
            switch.set_valign(Gtk.Align.CENTER)
            switch.get_style_context().add_class("editor-toggle")
            switch.set_active(values[key])
            row.pack_end(switch, False, False, 0)
            form.pack_start(row, False, False, 0)
            self.editor_fields[key] = switch

        advanced = Gtk.Expander(label="Подпись, значок и запуск")
        more = box(Gtk.Orientation.VERTICAL)
        text_field(more, "subtitle", "Подпись", "Автоматически по типу приложения", 200)
        text_field(more, "icon", "Значок", "Имя значка или путь к SVG / PNG", 4096)
        text_field(more, "command", "Команда", entry.executable or "Исходная команда", 4096,
                   "Программа и аргументы. Для shell-синтаксиса укажи sh -c '…'")
        text_field(more, "cwd", "Папка запуска", "Как у приложения", 4096)
        terminal_row = box(spacing=14, css="editor-row")
        caption = label("Терминал", "subtitle")
        caption.set_size_request(100, -1)
        terminal_row.pack_start(caption, False, False, 0)
        terminal = Gtk.ComboBoxText()
        for value, title in (("inherit", "Как у приложения"), ("true", "Запускать в терминале"), ("false", "Без терминала")):
            terminal.append(value, title)
        terminal.set_active_id("inherit" if values["terminal"] is None else str(values["terminal"]).lower())
        terminal.set_hexpand(True)
        terminal_row.pack_start(terminal, True, True, 0)
        more.pack_start(terminal_row, False, False, 0)
        self.editor_fields["terminal"] = terminal
        advanced.add(more)
        form.pack_start(advanced, False, False, 0)
        self.content.pack_start(form, False, False, 0)
        self.hints.set_text("ctrl+s  сохранить    alt+←  назад")
        self.content.show_all()
        self.content_scroller.get_vadjustment().set_value(0)
        self.editor_fields["name"].grab_focus()

    def save_app(self):
        if self.view != "app_edit": return
        fields = self.editor_fields
        values = {key: fields[key].get_text().strip() for key in ("name", "subtitle", "command", "cwd", "icon")}
        values["aliases"] = [item.strip() for item in re.split(r"[,;]", fields["aliases"].get_text()) if item.strip()]
        values.update({key: fields[key].get_active() for key in ("hidden", "pinned")})
        terminal = fields["terminal"].get_active_id()
        values["terminal"] = None if terminal == "inherit" else terminal == "true"
        try:
            self.config.update_app(self.editing_id, values)
        except (ValueError, OSError) as error:
            self.hints.set_text("Не сохранено: " + str(error).splitlines()[0].split(": ", 1)[-1])
            return
        self.catalog = Catalog(self.config.data)
        self.editor_back()
        self.hints.set_text("Настройки приложения сохранены · ctrl+e  изменить")

    def reset_app(self):
        if self.view != "app_edit": return
        app_id = self.editing_id
        try:
            self.config.reset_app(app_id)
        except OSError as error:
            self.hints.set_text(f"Не удалось сбросить: {error}")
            return
        self.catalog = Catalog(self.config.data)
        entry = next((item for item in self.catalog.all(include_hidden=True) if item.id == app_id), None)
        if entry:
            self.edit_app(entry)
            self.hints.set_text("Исходные настройки восстановлены")
        else:
            self.editor_back()

    def editor_back(self):
        self.entry.set_sensitive(True)
        self.save_button.hide()
        self.mode_hint.set_text("tab  режим")
        self.view = getattr(self, "editor_return_view", "main")
        self.placeholder.set_text(PLACEHOLDERS[self.mode])
        self.entry.set_text(getattr(self, "editor_query", ""))
        if self.view == "app_manager": self.app_manager_view()
        elif self.view == "hidden": self.render_hidden()
        else: self.render()
        self.entry.grab_focus()

    def settings(self):
        self.stop_work()
        self.entry.set_sensitive(False)
        self.save_button.hide()
        self.mode_hint.set_text("tab  поля")
        self.clear_content()
        self.view = "settings"
        self.entry.set_text("")
        self.placeholder.set_text("Настройки")
        self.section("НАСТРОЙКИ", "сохраняются сразу")
        content = box(Gtk.Orientation.VERTICAL, css="settings")
        self.build_mode_settings(content)
        ai = Gtk.Button(label="Подключение к ИИ…")
        ai.connect("clicked", lambda *_: self.ai_settings())
        content.pack_start(ai, False, False, 8)
        presets = box(spacing=8)
        presets.pack_start(label("Акцент", "subtitle"), True, True, 0)
        for name, accent in (("Lavender", DEFAULTS["theme"]["accent"]), ("Sage", "#a5c9ba"), ("Ice", "#a9c8e6")):
            item = Gtk.Button(label=name)
            item.set_can_focus(False)
            item.connect("clicked", lambda _, a=accent: self.update_setting("theme", "accent", a))
            presets.pack_start(item, False, False, 0)
        content.pack_start(presets, False, False, 8)
        for title, key, lo, hi, step in (("Ширина", "width", 480,1000,20), ("Скругление", "radius",0,32,1), ("Прозрачность", "opacity",.7,1,.01), ("Строки", "rows",3,10,1)):
            row = box(spacing=20, css="setting")
            title_label = label(title, "subtitle")
            title_label.set_size_request(120,-1)
            row.pack_start(title_label, False, False, 0)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,lo,hi,step)
            scale.set_hexpand(True)
            scale.set_value(self.config.data["window"][key])
            scale.set_digits(2 if key == "opacity" else 0)
            scale.set_can_focus(False)
            scale.connect("value-changed", lambda s,k=key: self.update_setting("window",k,round(s.get_value(),2) if k=="opacity" else int(s.get_value())))
            row.pack_start(scale, True, True, 0)
            content.pack_start(row, False, False, 0)
        bottom = box(spacing=8)
        applications = Gtk.Button(label="Настройки приложений…")
        applications.connect("clicked", lambda *_: self.app_manager())
        bottom.pack_start(applications, True, True, 0)
        back = Gtk.Button(label="Готово")
        back.get_style_context().add_class("primary")
        back.connect("clicked", lambda *_: self.set_mode(self.mode))
        bottom.pack_end(back, False, False, 0)
        content.pack_start(bottom, False, False, 12)
        self.content.pack_start(content, False, False, 0)
        self.hints.set_text("Остальные параметры: ~/.config/luch/config.toml")
        self.content.show_all()
        self.content_scroller.get_vadjustment().set_value(0)

    def build_mode_settings(self, content):
        sections = box(spacing=12, css="setting")
        sections.pack_start(label("Разделы", "subtitle"), True, True, 0)
        self.mode_checks = {}
        for mode in MODES:
            check = Gtk.CheckButton(label=NAMES[mode].capitalize() if mode != "shell" else "Shell")
            check.set_active(self.config.data["modes"][mode])
            check.connect("toggled", self.change_mode_visibility, mode)
            self.mode_checks[mode] = check
            sections.pack_start(check, False, False, 0)
        self.sync_mode_checks()
        content.pack_start(sections, False, False, 0)

        behavior = box(spacing=12, css="setting")
        behavior.pack_start(label("Win на последнем разделе", "subtitle"), True, True, 0)
        self.last_toggle_choice = Gtk.ComboBoxText()
        self.last_toggle_choice.append("wrap", "Перейти к первому")
        self.last_toggle_choice.append("close", "Закрыть окно")
        self.last_toggle_choice.set_active_id(self.config.data["modes"]["on_last_toggle"])
        self.last_toggle_choice.connect("changed", lambda choice: self.update_setting(
            "modes", "on_last_toggle", choice.get_active_id()))
        behavior.pack_start(self.last_toggle_choice, False, False, 0)
        content.pack_start(behavior, False, False, 0)

    def sync_mode_checks(self):
        enabled = self.enabled_modes()
        for mode, check in self.mode_checks.items():
            required = len(enabled) == 1 and mode in enabled
            check.set_sensitive(not required)
            check.set_tooltip_text("Нужен хотя бы один раздел" if required else None)

    def change_mode_visibility(self, check, mode):
        active = check.get_active()
        if not active and self.enabled_modes() == (mode,):
            check.set_active(True)
        elif active != self.config.data["modes"][mode]:
            self.update_setting("modes", mode, active)
            if self.mode not in self.enabled_modes():
                self.mode = self.enabled_modes()[0]
            self.sync_mode_buttons()
            self.sync_mode_checks()

    def ai_settings(self):
        self.stop_work()
        self.entry.set_sensitive(False)
        self.save_button.hide()
        self.clear_content()
        self.view = "ai_settings"
        self.entry.set_text("")
        self.placeholder.set_text("Подключение к ИИ")
        self.section("ПОДКЛЮЧЕНИЕ К ИИ", "OpenAI-совместимый API")
        form = box(Gtk.Orientation.VERTICAL, css="app-editor")
        presets = box(spacing=8)
        presets.pack_start(button("back", "Назад · Alt+←", self.settings, size=17), False, False, 0)
        for title, endpoint in (("Ollama", "http://127.0.0.1:11434/v1"),
                                ("OpenAI", "https://api.openai.com/v1"),
                                ("OpenRouter", "https://openrouter.ai/api/v1")):
            preset = Gtk.Button(label=title)
            preset.connect("clicked", lambda _, url=endpoint: self.ai_preset(url))
            presets.pack_start(preset, True, True, 0)
        form.pack_start(presets, False, False, 0)
        self.ai_fields = {}
        for key, title, placeholder in (("endpoint", "Адрес API", "https://example.com/v1"),
                                        ("api_key", "API-ключ", "Не нужен для локальной Ollama"),
                                        ("model", "Модель", "ID модели из выбранного сервиса")):
            row = box(spacing=14, css="editor-row")
            caption = label(title, "subtitle")
            caption.set_size_request(100, -1)
            row.pack_start(caption, False, False, 0)
            field = Gtk.Entry()
            field.get_style_context().add_class("preference-entry")
            field.set_width_chars(1)
            field.set_max_width_chars(1)
            field.set_max_length(4096 if key == "api_key" else 2048 if key == "endpoint" else 256)
            field.set_hexpand(True)
            field.set_placeholder_text(placeholder)
            field.set_text(self.config.data["ai"][key])
            if key == "api_key":
                field.set_visibility(False)
                field.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            field.connect("activate", lambda *_: self.save_ai_settings())
            row.pack_start(field, True, True, 0)
            form.pack_start(row, False, False, 0)
            self.ai_fields[key] = field
        description = label("Адрес включает /v1. Ключ хранится в личном файле настроек.", "subtitle")
        description.set_line_wrap(True)
        description.set_max_width_chars(45)
        form.pack_start(description, False, False, 0)
        actions = box(spacing=8)
        check = Gtk.Button(label="Проверить подключение")
        check.connect("clicked", lambda *_: self.check_ai_connection())
        actions.pack_start(check, True, True, 0)
        save = Gtk.Button(label="Сохранить")
        save.get_style_context().add_class("primary")
        save.connect("clicked", lambda *_: self.save_ai_settings())
        actions.pack_start(save, True, True, 0)
        form.pack_start(actions, False, False, 8)
        self.content.pack_start(form, False, False, 0)
        self.hints.set_text("ctrl+s  сохранить    alt+←  назад")
        self.content.show_all()
        self.content_scroller.get_vadjustment().set_value(0)
        self.ai_fields["endpoint"].grab_focus()

    def ai_preset(self, endpoint):
        current = urlsplit(self.ai_fields["endpoint"].get_text())
        selected = urlsplit(endpoint)
        if (current.scheme, current.netloc) != (selected.scheme, selected.netloc):
            self.ai_fields["api_key"].set_text("")
            self.ai_fields["model"].set_text(DEFAULT_MODEL if selected.hostname == "127.0.0.1" else "")
        self.ai_fields["endpoint"].set_text(endpoint)

    def ai_values(self):
        values = {key: field.get_text().strip() for key, field in self.ai_fields.items()}
        values["api_format"] = "openai"
        candidate = copy.deepcopy(self.config.data)
        candidate["ai"].update(values)
        errors = validate(candidate)
        if errors:
            raise ValueError(errors[0].split(": ", 1)[-1])
        return values

    def save_ai_settings(self):
        if self.view != "ai_settings": return
        try:
            self.config.update_ai(self.ai_values())
        except (ValueError, OSError) as error:
            self.hints.set_text("Не сохранено: " + str(error).splitlines()[0])
            return
        self.stop_work()
        self.client = AiClient(self.config.data)
        if "ai" in self.enabled_modes():
            self.set_mode("ai")
            self.hints.set_text("Подключение сохранено · введи вопрос")
        else:
            self.settings()
            self.hints.set_text("Подключение сохранено · раздел «Помощник» выключен")

    def check_ai_connection(self):
        try:
            values = self.ai_values()
        except ValueError as error:
            self.hints.set_text("Проверь настройки: " + str(error))
            return
        generation, cancel = self.start_work()
        self.hints.set_text("Проверяю подключение…")

        def worker():
            result = AiClient(values).check_connection()
            if not cancel.is_set():
                self.post(generation, self.ai_connection_checked, result)

        threading.Thread(target=worker, daemon=True).start()

    def ai_connection_checked(self, result):
        self.busy = False
        self.hints.set_text(result)

    def update_setting(self, section, key, value):
        self.config.data[section][key] = value
        self.config.save()
        if section != "modes": self.apply_theme()

    def hidden_view(self):
        self.stop_work()
        self.entry.set_sensitive(True)
        self.save_button.hide()
        self.mode_hint.set_text("tab  режим")
        self.entry.set_text("")
        self.render_hidden()
        self.entry.grab_focus()

    def focus_lost(self, *_):
        if self.visible and self.config.data["window"]["close_on_focus_loss"]:
            self.hide()
        return False

    def key_pressed(self, _, event):
        key = Gdk.keyval_name(event.keyval)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        if key == "Escape": self.hide(); return True
        if alt and key in ("1", "2", "3"):
            mode = MODES[int(key)-1]
            if mode in self.enabled_modes(): self.set_mode(mode)
            return True
        if alt and key == "Left":
            if self.view == "ai_settings": self.settings()
            elif self.view == "app_edit": self.editor_back()
            elif self.view == "settings": self.set_mode(self.mode)
            elif self.view in ("app_manager", "hidden"): self.set_mode("apps")
            return True
        if self.view in ("app_edit", "ai_settings"):
            if ctrl and (key.lower() == "s" or event.hardware_keycode == 39):
                if self.view == "ai_settings": self.save_ai_settings()
                else: self.save_app()
                return True
            if key in ("Return", "KP_Enter") and isinstance(self.window.get_focus(), Gtk.Entry):
                if self.view == "ai_settings": self.save_ai_settings()
                else: self.save_app()
                return True
            # Entry editing and ordinary GTK focus navigation own these keys.
            return False
        if ctrl and key in ("comma", "б"):
            self.settings(); return True
        if self.view == "settings":
            return False
        if key in ("Tab", "ISO_Left_Tab"):
            if self.view in ("hidden", "app_manager"): self.set_mode(self.mode)
            else: self.cycle_mode(-1 if shift or key == "ISO_Left_Tab" else 1)
            return True
        if key == "F2" or ctrl and (key.lower() == "e" or event.hardware_keycode == 26):
            if ctrl and shift: self.app_manager()
            else: self.edit_app()
            return True
        # Hardware codes preserve launcher shortcuts in the Russian layout.
        if ctrl and (key.lower() in ("h", "р") or event.hardware_keycode == 43):
            if shift: self.hidden_view()
            else: self.hide_selected()
            return True
        if ctrl and key == "BackSpace" and self.pending_command:
            self.deny_command(); return True
        if key == "Down": self.move_selection(1); return True
        if key == "Up": self.move_selection(-1); return True
        if shift and key in ("Return", "KP_Enter") and self.mode == "shell":
            self.activate(terminal=True); return True
        if key in ("Return", "KP_Enter"):
            self.activate(); return True
        return False


def run(on_ready=None):
    launcher = Launcher()
    if on_ready: on_ready(launcher)
    Gtk.main()
