"""AI connection preferences through real offscreen GTK widgets."""

from unittest.mock import patch
from types import SimpleNamespace

import copy

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk

from test_ui_geometry import OffscreenLauncherTest
from luch.config import Config


class AiSettingsTest(OffscreenLauncherTest):
    def test_save_connection_preserves_other_preferences_and_masks_key(self):
        launcher = self.launcher
        original = copy.deepcopy(launcher.config.data)
        size = self.size()
        launcher.ai_settings()
        self.assertEqual(self.size(), size)
        fields = launcher.ai_fields
        self.assertFalse(fields["api_key"].get_visibility())
        fields["endpoint"].set_text("https://example.test/api/v1")
        fields["api_key"].set_text("private-api-key")
        fields["model"].set_text("account/model")
        launcher.save_ai_settings()
        loaded = Config(launcher.config.path)
        self.assertEqual(loaded.data["ai"]["endpoint"], "https://example.test/api/v1")
        self.assertEqual(loaded.data["ai"]["api_key"], "private-api-key")
        self.assertEqual(loaded.data["ai"]["api_format"], "openai")
        self.assertEqual(loaded.path.stat().st_mode & 0o777, 0o600)
        for section in ("window", "theme", "apps", "shell"):
            self.assertEqual(loaded.data[section], original[section])
        self.assertEqual(launcher.client.endpoint, "https://example.test/api/v1")
        self.assertEqual(launcher.client.messages, [])
        self.assertEqual(launcher.mode, "ai")
        self.assertNotIn("private-api-key", launcher.hints.get_text())

    def test_invalid_url_is_not_saved(self):
        launcher = self.launcher
        original = copy.deepcopy(launcher.config.data)
        launcher.ai_settings()
        launcher.ai_fields["endpoint"].set_text("file:///tmp/not-api")
        launcher.save_ai_settings()
        self.assertEqual(launcher.view, "ai_settings")
        self.assertEqual(launcher.config.data, original)
        self.assertTrue(launcher.hints.get_text().startswith("Не сохранено:"))

    def test_provider_preset_clears_other_providers_key_and_model(self):
        launcher = self.launcher
        launcher.ai_settings()
        launcher.ai_fields["api_key"].set_text("private-old-provider-key")
        launcher.ai_preset("https://api.openai.com/v1")
        self.assertEqual(launcher.ai_fields["api_key"].get_text(), "")
        self.assertEqual(launcher.ai_fields["model"].get_text(), "")
        self.assertEqual(launcher.config.data["ai"]["endpoint"], "http://127.0.0.1:11434/v1")

    def test_check_connection_runs_in_worker_without_saving_draft(self):
        launcher = self.launcher
        launcher.ai_settings()
        original = copy.deepcopy(launcher.config.data)
        launcher.ai_fields["endpoint"].set_text("https://example.test/v1")
        with patch("luch.ui.threading.Thread") as thread:
            launcher.check_ai_connection()
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(launcher.config.data, original)
        with patch("luch.ui.AiClient.check_connection", return_value="Подключение работает") as check:
            thread.call_args.kwargs["target"]()
            self.settle()
        check.assert_called_once_with()
        self.assertEqual(launcher.hints.get_text(), "Подключение работает")

    def test_tab_and_arrows_edit_fields_and_ctrl_s_saves(self):
        launcher = self.launcher
        launcher.ai_settings()
        for key in ("Tab", "Up", "Down"):
            event = SimpleNamespace(keyval=Gdk.keyval_from_name(key), state=0, hardware_keycode=0)
            self.assertFalse(launcher.key_pressed(None, event))
        event = SimpleNamespace(keyval=Gdk.KEY_s, state=Gdk.ModifierType.CONTROL_MASK, hardware_keycode=39)
        self.assertTrue(launcher.key_pressed(None, event))
        self.assertEqual(launcher.view, "main")
        self.assertEqual(launcher.mode, "ai")

    def test_small_window_keeps_geometry_with_long_api_fields(self):
        launcher = self.launcher
        launcher.config.data["window"].update(width=480, rows=4)
        launcher.apply_theme()
        expected = self.size()
        launcher.ai_settings()
        launcher.ai_fields["endpoint"].set_text("https://example.test/" + "a" * 1000)
        self.assertEqual(self.size(), expected)

