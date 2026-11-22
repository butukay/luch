from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from luch.catalog import AppEntry, Catalog, match_score


def desktop(app_id, name, *, visible=True, executable="", terminal=False):
    info = Mock()
    info.should_show.return_value = visible
    info.get_id.return_value = app_id
    info.get_name.return_value = name
    info.get_display_name.return_value = name
    info.get_description.return_value = ""
    info.get_executable.return_value = executable
    info.get_icon.return_value = None
    info.get_keywords.return_value = []
    info.get_categories.return_value = ""
    info.get_filename.return_value = f"/usr/share/applications/{app_id}"
    info.get_string.side_effect = lambda key: {"Exec": executable or app_id.removesuffix(".desktop")}.get(key)
    info.get_boolean.side_effect = lambda key: terminal if key == "Terminal" else False
    info.launch.return_value = True
    return info


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "apps.json"
        self.chrome = desktop("google-chrome.desktop", "Google Chrome", executable="google-chrome")
        self.kitty = desktop("kitty.desktop", "kitty", executable="kitty")
        self.files = desktop("thunar.desktop", "Thunar File Manager", executable="thunar")

    def catalogue(self, config=None, infos=None):
        with patch("luch.catalog.Gio.AppInfo.get_all", return_value=infos or [self.chrome, self.kitty, self.files]):
            return Catalog(config, state_path=self.state)

    def test_ignores_non_launcher_entries_and_deduplicates_ids(self):
        invisible = desktop("agent.desktop", "Background Agent", visible=False)
        duplicate = desktop("kitty.desktop", "Duplicate")
        catalogue = self.catalogue(infos=[self.kitty, invisible, duplicate])
        self.assertEqual([entry.name for entry in catalogue.all()], ["kitty"])

    def test_hidden_apps_are_removed_from_both_browse_and_search(self):
        catalogue = self.catalogue({"apps": {"hidden": ["kitty.desktop"]}})
        self.assertNotIn("kitty.desktop", [entry.id for entry in catalogue.search("")])
        self.assertEqual(catalogue.search("kitty"), [])
        self.assertEqual(len(catalogue.all(include_hidden=True)), 3)
        catalogue.set_hidden([])
        self.assertEqual(catalogue.search("kitty")[0].id, "kitty.desktop")

    def test_favorites_follow_configuration(self):
        catalogue = self.catalogue({"apps": {"favorites": ["thunar.desktop", "kitty.desktop"]}})
        self.assertEqual([entry.id for entry in catalogue.search("")[:2]], ["thunar.desktop", "kitty.desktop"])

    def test_russian_aliases_and_wrong_keyboard_layout(self):
        catalogue = self.catalogue()
        for query in ("браузер", "хром", "chr", "сркщьу"):
            with self.subTest(query=query):
                self.assertEqual(catalogue.search(query)[0].id, "google-chrome.desktop")
        self.assertEqual(catalogue.search("файлы")[0].id, "thunar.desktop")
        self.assertEqual(catalogue.search("терминал")[0].id, "kitty.desktop")

    def test_exact_name_beats_subsequence(self):
        exact = AppEntry("code.desktop", "Code")
        fuzzy = AppEntry("other.desktop", "Cool Dev")
        self.assertGreater(match_score(exact, "code"), match_score(fuzzy, "code"))

    def test_primary_app_beats_its_helpers_for_semantic_alias(self):
        helper = desktop("thunar-bulk-rename.desktop", "Bulk Rename", executable="thunar")
        catalogue = self.catalogue(infos=[helper, self.files])
        self.assertEqual(catalogue.search("файлы")[0].id, "thunar.desktop")

    def test_launch_records_usage_and_recent_app_comes_first(self):
        catalogue = self.catalogue()
        catalogue.launch(catalogue.search("thunar")[0])
        self.files.launch.assert_called_once_with([], None)
        self.assertEqual(catalogue.search("")[0].id, "thunar.desktop")
        self.assertEqual(self.catalogue().search("")[0].id, "thunar.desktop")

    def test_failed_launch_does_not_record_usage(self):
        self.files.launch.return_value = False
        catalogue = self.catalogue()
        with self.assertRaises(RuntimeError):
            catalogue.launch(catalogue.search("thunar")[0])
        self.assertFalse(self.state.exists())

    def test_terminal_field_codes_are_expanded_without_shell(self):
        terminal = desktop("nvim.desktop", "Neovim", executable="nvim", terminal=True)
        terminal.get_string.side_effect = lambda key: {
            "Exec": "nvim --servername='hello world' %F %% %c %k %i", "Icon": "nvim", "Path": "/tmp"
        }.get(key)
        terminal.get_filename.return_value = "/usr/share/applications/nvim.desktop"
        catalogue = self.catalogue({"shell": {"terminal": "kitty --class luch-shell"}}, infos=[terminal])
        with patch("luch.catalog.subprocess.Popen") as popen:
            catalogue.launch(catalogue.all()[0])
        self.assertEqual(popen.call_args.args[0], [
            "kitty", "--class", "luch-shell", "-e", "nvim", "--servername=hello world", "%", "Neovim",
            "/usr/share/applications/nvim.desktop", "--icon", "nvim",
        ])
        self.assertEqual(popen.call_args.kwargs["cwd"], "/tmp")

    def test_invalid_history_does_not_prevent_startup(self):
        self.state.write_text("{broken")
        self.assertEqual(len(self.catalogue().all()), 3)

    def test_custom_name_and_aliases_retain_original_name_search(self):
        catalogue = self.catalogue({"apps": {"overrides": {
            "google-chrome.desktop": {"name": "Работа", "aliases": ["веб", "kitty"], "subtitle": "Профиль офиса"}
        }}})
        entry = catalogue.search("Работа")[0]
        self.assertEqual(entry.name, "Работа")
        self.assertEqual(entry.original_name, "Google Chrome")
        self.assertEqual(entry.subtitle, "Профиль офиса")
        for query in ("Google Chrome", "веб", "kitty"):
            self.assertEqual(catalogue.search(query)[0].id, entry.id, query)

    def test_explicit_pin_precedes_recent_but_does_not_override_search_relevance(self):
        catalogue = self.catalogue({"apps": {"overrides": {"google-chrome.desktop": {"pinned": True}}}})
        catalogue.launch(catalogue.search("thunar")[0])
        self.assertEqual(catalogue.search("")[0].id, "google-chrome.desktop")
        self.assertEqual(catalogue.search("thunar")[0].id, "thunar.desktop")

    def test_profile_hidden_flag_overrides_old_hidden_list_in_both_directions(self):
        catalogue = self.catalogue({"apps": {"hidden": ["google-chrome.desktop"], "overrides": {
            "google-chrome.desktop": {"hidden": False}, "kitty.desktop": {"hidden": True}
        }}})
        self.assertEqual(catalogue.hidden, {"kitty.desktop"})
        self.assertTrue(catalogue.search("chrome"))
        self.assertFalse(catalogue.search("kitty"))
        self.assertEqual(catalogue.search("kitty", hidden=[])[0].id, "kitty.desktop")

    def test_custom_launch_arguments_and_working_directory_do_not_invoke_shell(self):
        catalogue = self.catalogue({"apps": {"overrides": {"kitty.desktop": {
            "command": "tool --title 'hello world' '$HOME'", "cwd": "~/Documents", "terminal": True
        }}}, "shell": {"terminal": "kitty --class custom-terminal"}})
        with patch("luch.catalog.subprocess.Popen") as popen:
            catalogue.launch(catalogue.search("kitty")[0])
        self.assertEqual(popen.call_args.args[0], ["kitty", "--class", "custom-terminal", "-e", "tool", "--title", "hello world", "$HOME"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(Path.home() / "Documents"))
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.kitty.launch.assert_not_called()

    def test_terminal_override_can_disable_desktop_terminal_flag(self):
        terminal = desktop("nvim.desktop", "Neovim", executable="nvim", terminal=True)
        terminal.get_string.side_effect = lambda key: {"Exec": "nvim %F", "Path": "/tmp"}.get(key)
        catalogue = self.catalogue({"apps": {"overrides": {"nvim.desktop": {"terminal": False}}}}, infos=[terminal])
        with patch("luch.catalog.subprocess.Popen") as popen:
            catalogue.launch(catalogue.all()[0])
        self.assertEqual(popen.call_args.args[0], ["nvim"])
        self.assertEqual(popen.call_args.kwargs["cwd"], "/tmp")

    def test_icon_override_is_explicit_and_does_not_change_app_identity(self):
        catalogue = self.catalogue({"apps": {"overrides": {"google-chrome.desktop": {"icon": "custom-browser"}}}})
        entry = catalogue.search("chrome")[0]
        self.assertEqual(entry.icon_override, "custom-browser")
        self.assertIn("custom-browser", entry.icon.get_names())
        self.assertEqual(entry.id, "google-chrome.desktop")


if __name__ == "__main__":
    unittest.main()
