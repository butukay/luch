"""Per-app editing through real offscreen GTK widgets and temporary settings."""
from types import SimpleNamespace
from unittest.mock import patch

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

from luch.catalog import Catalog
from luch.config import Config
from test_ui_geometry import OffscreenLauncherTest


class AppEditorTest(OffscreenLauncherTest):
    def app(self):
        entries = self.launcher.catalog.all(include_hidden=True)
        if not entries: self.skipTest("No installed desktop entries")
        return next((entry for entry in entries if entry.id == "google-chrome.desktop"), entries[0])

    def test_save_name_aliases_visibility_and_pin_persists_without_changing_geometry(self):
        launcher, entry = self.launcher, self.app()
        expected = self.size()
        launcher.edit_app(entry)
        fields = launcher.editor_fields
        self.assertEqual(fields["name"].get_text(), entry.original_name)
        fields["name"].set_text("Мой браузер")
        fields["aliases"].set_text("web-work, рабочий браузер; web-work")
        fields["hidden"].set_active(True)
        fields["pinned"].set_active(True)
        launcher.save_app()
        self.assertEqual(self.size(), expected)
        stored = Config(launcher.config.path)
        settings = stored.app_settings(entry.id)
        self.assertEqual(settings["name"], "Мой браузер")
        self.assertEqual(settings["aliases"], ["web-work", "рабочий браузер"])
        self.assertTrue(settings["hidden"])
        self.assertTrue(settings["pinned"])
        self.assertEqual(launcher.view, "main")
        self.assertFalse(launcher.save_button.get_visible())
        self.assertNotIn(entry.id, [item.id for item in launcher.catalog.search("web-work", limit=100)])
        self.assertEqual(launcher.catalog.search("web-work", hidden=[])[0].id, entry.id)

    def test_manager_includes_hidden_apps_and_enter_edits_instead_of_launching(self):
        launcher, entry = self.launcher, self.app()
        launcher.config.update_app(entry.id, {"hidden": True, "aliases": ["hidden-work"]})
        launcher.catalog = Catalog(launcher.config.data)
        launcher.app_manager()
        launcher.entry.set_text("hidden-work")
        self.assertEqual(launcher.results[0].id, entry.id)
        with patch("luch.catalog.Catalog.launch") as launch:
            launcher.activate()
            launch.assert_not_called()
        self.assertEqual(launcher.view, "app_edit")
        self.assertTrue(launcher.editor_fields["hidden"].get_active())
        launcher.editor_fields["hidden"].set_active(False)
        launcher.save_app()
        self.assertEqual(launcher.view, "app_manager")
        self.assertNotIn(entry.id, launcher.catalog.hidden)
        self.assertEqual(launcher.results[0].id, entry.id)

    def test_reset_restores_original_values_and_unhides_the_selected_app(self):
        launcher, entry = self.launcher, self.app()
        launcher.config.update_app(entry.id, {"name": "Custom", "hidden": True, "pinned": True,
            "aliases": ["reset-me"], "command": "echo custom", "icon": "my-icon"})
        launcher.catalog = Catalog(launcher.config.data)
        changed = next(item for item in launcher.catalog.all(include_hidden=True) if item.id == entry.id)
        launcher.edit_app(changed)
        launcher.reset_app()
        self.assertEqual(launcher.view, "app_edit")
        self.assertEqual(launcher.editor_fields["name"].get_text(), entry.original_name)
        self.assertEqual(launcher.editor_fields["aliases"].get_text(), "")
        self.assertFalse(launcher.editor_fields["hidden"].get_active())
        self.assertFalse(launcher.editor_fields["pinned"].get_active())
        self.assertNotIn(entry.id, Config(launcher.config.path).data["apps"]["overrides"])

    def test_invalid_command_keeps_editor_open_and_existing_preferences_intact(self):
        launcher, entry = self.launcher, self.app()
        launcher.config.update_app(entry.id, {"name": "Saved name"})
        launcher.edit_app(entry)
        launcher.editor_fields["name"].set_text("Unsaved name")
        launcher.editor_fields["command"].set_text('tool "unterminated')
        launcher.save_app()
        self.assertEqual(launcher.view, "app_edit")
        self.assertEqual(Config(launcher.config.path).app_settings(entry.id)["name"], "Saved name")
        self.assertTrue(launcher.hints.get_text().startswith("Не сохранено:"))

    def test_editor_tab_and_arrow_keys_stay_with_fields_and_ctrl_s_saves(self):
        launcher = self.launcher
        launcher.edit_app(self.app())
        for key in (Gdk.KEY_Tab, Gdk.KEY_Down, Gdk.KEY_Up, Gdk.KEY_Left):
            event = SimpleNamespace(keyval=key, state=Gdk.ModifierType(0), hardware_keycode=0)
            self.assertFalse(launcher.key_pressed(launcher.window, event))
            self.assertEqual(launcher.view, "app_edit")
        event = SimpleNamespace(keyval=Gdk.KEY_s, state=Gdk.ModifierType.CONTROL_MASK, hardware_keycode=39)
        self.assertTrue(launcher.key_pressed(launcher.window, event))
        self.assertEqual(launcher.view, "main")

    def test_advanced_fields_scroll_without_changing_window_and_footer_has_no_brand(self):
        launcher = self.launcher
        expected = self.size()
        launcher.edit_app(self.app())
        form = launcher.content.get_children()[-1]
        expander = next(child for child in form.get_children() if isinstance(child, Gtk.Expander))
        expander.set_expanded(True)
        self.assertEqual(self.size(), expected)
        adjustment = launcher.content_scroller.get_vadjustment()
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())
        def texts(widget):
            if isinstance(widget, Gtk.Label): yield widget.get_text().casefold()
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children(): yield from texts(child)
        self.assertNotIn("луч", list(texts(launcher.frame)))
