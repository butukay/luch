import copy
from datetime import date, datetime, time, timezone
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from luch.config import Config, DEFAULTS, validate


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "luch/config.toml"

    def config_text(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        return Config(self.path)

    def test_missing_file_uses_independent_defaults(self):
        config = Config(self.path)
        self.assertEqual(config.data, DEFAULTS)
        config.data["apps"]["hidden"].append("kitty.desktop")
        self.assertEqual(DEFAULTS["apps"]["hidden"], [])
        self.assertEqual(Config(self.path).errors, [])

    def test_theme_inherits_unset_colors_from_defaults(self):
        config = self.config_text('[theme]\naccent = "#a9c8e6"\n')
        self.assertEqual(config.errors, [])
        self.assertEqual(config.data["theme"], {**DEFAULTS["theme"], "accent": "#a9c8e6"})

    def test_existing_native_ollama_config_keeps_its_protocol(self):
        config = self.config_text('[ai]\nendpoint = "http://127.0.0.1:11435"\n')
        self.assertEqual(config.data["ai"]["api_format"], "ollama")
        config.update_ai({"endpoint": "http://127.0.0.1:11434/v1", "api_format": "openai"})
        self.assertEqual(Config(self.path).data["ai"]["api_format"], "openai")

    def test_scalar_sections_are_repaired_and_reported(self):
        config = self.config_text('window = "oops"\nai = false\napps = [1]\ntheme = 42\nshell = nan')
        self.assertEqual(config.data, DEFAULTS)
        self.assertEqual(len(config.errors), 5)

    def test_wrong_types_do_not_crash_or_reach_runtime(self):
        config = self.config_text('''
[window]
width = true
rows = "7"
radius = []
position = {}
font = 3
monitor = false
close_on_focus_loss = "false"
[theme]
accent = "red"
text = []
[apps]
hidden = ["kitty.desktop", 3]
favorites = "kitty.desktop"
icon_style = 2
[shell]
cwd = []
terminal = false
timeout = true
[ai]
endpoint = []
model = 4
keep_alive = false
num_ctx = true
num_predict = "384"
temperature = {}
timeout = false
''')
        self.assertEqual(config.data, DEFAULTS)
        self.assertGreater(len(config.errors), 15)

    def test_nonfinite_out_of_range_and_fractional_integer_values(self):
        for section, key in (("window", "width"), ("window", "opacity"),
                             ("shell", "timeout"), ("ai", "num_ctx"),
                             ("ai", "num_predict"), ("ai", "temperature"),
                             ("ai", "timeout"), ("ai", "keep_alive")):
            for value in (float("nan"), float("inf"), -float("inf"), 10 ** 500):
                with self.subTest(section=section, key=key, value=str(value)[:40]):
                    data = copy.deepcopy(DEFAULTS)
                    data[section][key] = value
                    self.assertTrue(validate(data))
                    self.assertEqual(data[section][key], DEFAULTS[section][key])
        config = self.config_text('[window]\nrows = 4.5\nwidth = 700.0\n[ai]\nnum_ctx = 4096.5')
        self.assertEqual(config.data["window"]["rows"], 7)
        self.assertIsInstance(config.data["window"]["width"], int)
        self.assertEqual(config.data["ai"]["num_ctx"], 4096)

    def test_endpoint_validation_and_custom_reverse_proxy(self):
        valid_urls = ("http://127.0.0.1:11435", "https://ollama.example.org/llm", "http://[::1]:11435/")
        invalid_urls = ("file:///tmp/model", "ftp://host", "http://", "http://host:99999", "http://host:0",
                        "http://user:pass@host", "http://@host", "http://host?query=1", "http://host/#fragment",
                        "http://some host", "http://[broken")
        for url in valid_urls + invalid_urls:
            with self.subTest(url=url):
                data = copy.deepcopy(DEFAULTS)
                data["ai"]["endpoint"] = url
                errors = validate(data)
                if url in valid_urls:
                    self.assertEqual(errors, [])
                    self.assertEqual(data["ai"]["endpoint"], url)
                else:
                    self.assertTrue(errors)
                    self.assertEqual(data["ai"]["endpoint"], DEFAULTS["ai"]["endpoint"])

    def test_model_names_and_keep_alive_durations(self):
        for model in ("qwen3:4b", "hf.co/Qwen/Qwen3-GGUF:Q4_K_M", "namespace/model-name"):
            data = copy.deepcopy(DEFAULTS)
            data["ai"]["model"] = model
            self.assertEqual(validate(data), [])
        for value in (-1, 0, 3600, 0.5, "0", "5m", "1h30m", "24h", "250ms", "1µs", "-1"):
            with self.subTest(keep_alive=value):
                data = copy.deepcopy(DEFAULTS)
                data["ai"]["keep_alive"] = value
                self.assertEqual(validate(data), [])
                self.assertEqual(data["ai"]["keep_alive"], -1 if value == "-1" else value)
        for value in (-2, "forever", "-5m", "99999999999h", [], None, "5m garbage"):
            with self.subTest(invalid_keep_alive=value):
                data = copy.deepcopy(DEFAULTS)
                data["ai"]["keep_alive"] = value
                self.assertTrue(validate(data))
                self.assertEqual(data["ai"]["keep_alive"], -1)

    def test_font_css_and_shell_invalid_quoting_are_repaired(self):
        data = copy.deepcopy(DEFAULTS)
        data["window"]["font"] = 'Sans"; color:red; /*'
        data["shell"]["terminal"] = 'kitty "'
        data["shell"]["cwd"] = "folder\0name"
        errors = validate(data)
        self.assertEqual(len(errors), 3)
        self.assertEqual(data, DEFAULTS)

    def test_round_trip_preserves_customization_and_unknown_toml_data(self):
        config = self.config_text('''
name = "мой лаунчер"
release = 2026-10-28
[window]
width = 860
close_on_focus_loss = true
[theme]
accent = "#a5c9ba"
[ai]
keep_alive = "1h30m"
[custom]
"dotted.key" = { "вложено" = [1, 2, 3], enabled = false }
entries = [{name = "one"}, {name = "two"}]
''')
        config.data["custom"]["when"] = datetime(2026, 10, 28, 10, 25, tzinfo=timezone.utc)
        config.data["custom"]["time"] = time(10, 25)
        before = copy.deepcopy(config.data)
        config.hide("kitty.desktop")
        before["apps"]["hidden"] = ["kitty.desktop"]
        parsed = tomllib.loads(self.path.read_text())
        self.assertEqual(parsed, before)
        self.assertEqual(Config(self.path).errors, [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_toml_is_reported_with_defaults(self):
        config = self.config_text("[window\nwidth = 800")
        self.assertEqual(config.data, DEFAULTS)
        self.assertEqual(len(config.errors), 1)

    def test_atomic_save_failure_preserves_original_and_cleans_temporary(self):
        config = self.config_text("[window]\nwidth = 800\n")
        original = self.path.read_bytes()
        with patch("luch.config.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                config.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".config-*")), [])

    def test_hide_unhide_preserves_other_settings_and_deduplicates(self):
        config = self.config_text('[window]\nwidth = 820\n[apps]\nhidden = ["kitty.desktop", "kitty.desktop"]')
        self.assertEqual(config.data["apps"]["hidden"], ["kitty.desktop"])
        config.hide("kitty.desktop")
        config.unhide("kitty.desktop")
        loaded = Config(self.path)
        self.assertEqual(loaded.data["window"]["width"], 820)
        self.assertEqual(loaded.data["apps"]["hidden"], [])

    def test_app_profile_defaults_inherit_hidden_without_pinning_recommendations(self):
        config = self.config_text('[apps]\nhidden = ["kitty.desktop"]')
        settings = config.app_settings("kitty.desktop")
        self.assertTrue(settings["hidden"])
        self.assertFalse(settings["pinned"])
        self.assertIsNone(settings["terminal"])
        settings["aliases"].append("temporary")
        self.assertEqual(config.app_settings("kitty.desktop")["aliases"], [])

    def test_app_profile_round_trip_preserves_other_apps_and_extra_fields(self):
        config = self.config_text('''
[apps.overrides."kitty.desktop"]
name = "Консоль"
aliases = ["term"]
custom_note = { enabled = true }
[apps.overrides."steam.desktop"]
hidden = true
''')
        config.update_app("kitty.desktop", {
            "name": "Терминал", "aliases": [" shell ", "консоль", "shell"],
            "subtitle": "Рабочая сессия", "pinned": True, "hidden": False,
            "command": 'kitty --title "Работа"', "cwd": "~/Projects",
            "terminal": False, "icon": "utilities-terminal-symbolic",
        })
        loaded = Config(self.path)
        self.assertEqual(loaded.errors, [])
        settings = loaded.app_settings("kitty.desktop")
        self.assertEqual(settings["name"], "Терминал")
        self.assertEqual(settings["aliases"], ["shell", "консоль"])
        self.assertEqual(settings["command"], 'kitty --title "Работа"')
        self.assertEqual(settings["custom_note"], {"enabled": True})
        self.assertTrue(loaded.app_settings("steam.desktop")["hidden"])
        self.assertEqual(loaded.data["window"], DEFAULTS["window"])

    def test_invalid_profile_updates_leave_memory_and_file_unchanged(self):
        config = Config(self.path)
        config.update_app("kitty.desktop", {"name": "Консоль", "aliases": ["term"]})
        original_data = copy.deepcopy(config.data)
        original_file = self.path.read_bytes()
        invalid = (
            {"name": 3}, {"name": "x" * 201}, {"subtitle": "one\ntwo"},
            {"aliases": "term"}, {"aliases": [""]}, {"aliases": ["a"] * 65},
            {"pinned": "true"}, {"hidden": 1}, {"terminal": "inherit"},
            {"command": 'kitty "'}, {"cwd": "bad\0path"}, {"icon": []},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    config.update_app("kitty.desktop", values)
                self.assertEqual(config.data, original_data)
                self.assertEqual(self.path.read_bytes(), original_file)
        for app_id in ("../kitty.desktop", "kitty", "bad\\kitty.desktop"):
            with self.subTest(app_id=app_id), self.assertRaises(ValueError):
                config.update_app(app_id, {"name": "Changed"})

    def test_malformed_profile_fields_are_repaired_without_dropping_valid_extras(self):
        data = copy.deepcopy(DEFAULTS)
        data["apps"]["overrides"] = {
            "../bad.desktop": {"name": "bad"},
            "bad.desktop": "wrong table",
            "kitty.desktop": {"name": [], "aliases": [""], "pinned": "true",
                              "terminal": 0, "command": 'kitty "', "custom": {"future": True}},
        }
        errors = validate(data)
        self.assertGreaterEqual(len(errors), 7)
        self.assertEqual(data["apps"]["overrides"], {"kitty.desktop": {"custom": {"future": True}}})

    def test_hide_and_unhide_sync_legacy_and_per_app_settings(self):
        config = self.config_text('''
[apps]
hidden = ["kitty.desktop"]
[apps.overrides."kitty.desktop"]
hidden = false
name = "Консоль"
''')
        self.assertFalse(config.app_settings("kitty.desktop")["hidden"])
        config.hide("kitty.desktop")
        self.assertTrue(Config(self.path).app_settings("kitty.desktop")["hidden"])
        self.assertEqual(config.data["apps"]["hidden"], ["kitty.desktop"])
        config.unhide("kitty.desktop")
        loaded = Config(self.path)
        self.assertFalse(loaded.app_settings("kitty.desktop")["hidden"])
        self.assertEqual(loaded.data["apps"]["hidden"], [])
        self.assertEqual(loaded.app_settings("kitty.desktop")["name"], "Консоль")

    def test_reset_app_restores_inheritance_and_preserves_other_preferences(self):
        config = Config(self.path)
        config.update_app("kitty.desktop", {"name": "Консоль", "hidden": True, "pinned": True})
        config.update_app("steam.desktop", {"name": "Игры", "hidden": True})
        favorites = list(config.data["apps"]["favorites"])
        config.reset_app("kitty.desktop")
        loaded = Config(self.path)
        self.assertNotIn("kitty.desktop", loaded.data["apps"]["overrides"])
        self.assertFalse(loaded.app_settings("kitty.desktop")["hidden"])
        self.assertFalse(loaded.app_settings("kitty.desktop")["pinned"])
        self.assertEqual(loaded.app_settings("kitty.desktop")["name"], "")
        self.assertIsNone(loaded.app_settings("kitty.desktop")["terminal"])
        self.assertTrue(loaded.app_settings("steam.desktop")["hidden"])
        self.assertEqual(loaded.app_settings("steam.desktop")["name"], "Игры")
        self.assertEqual(loaded.data["apps"]["favorites"], favorites)

    def test_profile_none_restores_inherited_fields(self):
        config = Config(self.path)
        config.update_app("kitty.desktop", {"name": "Консоль", "terminal": True, "aliases": ["term"]})
        config.update_app("kitty.desktop", {"name": None, "terminal": None})
        loaded = Config(self.path)
        self.assertEqual(loaded.app_settings("kitty.desktop")["name"], "")
        self.assertIsNone(loaded.app_settings("kitty.desktop")["terminal"])
        self.assertEqual(loaded.app_settings("kitty.desktop")["aliases"], ["term"])

    def test_failed_profile_save_rolls_back_data_and_retains_original_file(self):
        config = Config(self.path)
        config.update_app("kitty.desktop", {"name": "Консоль"})
        original_data, original_file = copy.deepcopy(config.data), self.path.read_bytes()
        with patch("luch.config.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                config.update_app("kitty.desktop", {"name": "Changed", "pinned": True})
        self.assertEqual(config.data, original_data)
        self.assertEqual(self.path.read_bytes(), original_file)
        self.assertEqual(list(self.path.parent.glob(".config-*")), [])

    def test_failed_profile_reset_keeps_live_and_saved_preferences_consistent(self):
        config = Config(self.path)
        config.update_app("kitty.desktop", {"name": "Консоль", "hidden": True})
        original_data, original_file = copy.deepcopy(config.data), self.path.read_bytes()
        with patch("luch.config.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                config.reset_app("kitty.desktop")
        self.assertEqual(config.data, original_data)
        self.assertEqual(self.path.read_bytes(), original_file)


if __name__ == "__main__":
    unittest.main()
