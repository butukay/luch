"""Allocate real GTK views offscreen; never open a visible launcher window."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import Mock, patch

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from luch.config import Config
from luch.ui import Launcher, MODES


@unittest.skipUnless(Gtk.init_check()[0], "GTK display unavailable")
class OffscreenLauncherTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(warnings.catch_warnings())
        warnings.simplefilter("ignore", DeprecationWarning)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        config = Config(Path(directory) / "config.toml")
        self.stack.enter_context(patch.dict(os.environ, {"XDG_STATE_HOME": directory}))
        self.stack.enter_context(patch("luch.ui.Config", return_value=config))
        self.stack.enter_context(patch("luch.ui.Gtk.Window", Gtk.OffscreenWindow))
        self.stack.enter_context(patch("luch.ui.GtkLayerShell.is_supported", return_value=False))
        self.stack.enter_context(patch("luch.ui.GtkLayerShell.is_layer_window", return_value=False))
        self.stack.enter_context(patch("luch.ui.Gio.AppInfoMonitor.get", return_value=Mock()))
        self.launcher = Launcher()
        self.launcher.visible = True
        self.launcher.window.show_all()
        self.settle()

    def tearDown(self):
        self.launcher.stop_work()
        Gtk.StyleContext.remove_provider_for_screen(self.launcher.window.get_screen(), self.launcher.css)
        self.launcher.window.destroy()
        self.settle()

    @staticmethod
    def settle():
        for _ in range(4):
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(.01)

    def size(self):
        self.settle()
        return tuple(self.launcher.window.get_size())


class UiGeometryTest(OffscreenLauncherTest):
    def test_mode_cycles_settings_and_hidden_manager_keep_identical_geometry(self):
        expected = self.size()
        self.assertEqual(expected, (700, 532))
        for _ in range(2):
            for mode in MODES:
                self.launcher.set_mode(mode)
                self.assertEqual(self.size(), expected, mode)
        self.launcher.settings()
        self.assertEqual(self.size(), expected)
        self.launcher.hidden_view()
        self.assertEqual(self.size(), expected)

    def test_long_input_status_and_streamed_output_scroll_without_resizing(self):
        expected = self.size()
        self.launcher.set_mode("shell")
        self.launcher.entry.set_text("an-unbroken-command-" * 200)
        self.launcher.hints.set_text("A detailed failure explanation " * 100)
        self.assertEqual(self.size(), expected)
        self.launcher.answer_view("A long request " * 100)
        self.launcher.answer_label.set_text(("unbroken-output-" * 12 + "\n") * 40)
        self.assertEqual(self.size(), expected)
        adjustment = self.launcher.content_scroller.get_vadjustment()
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())

    def test_custom_dimensions_stay_constant_with_short_or_large_mode_content(self):
        self.launcher.config.data["window"].update(width=480, rows=4)
        self.launcher.apply_theme()
        self.launcher.set_mode("apps")
        expected = self.size()
        self.assertEqual(expected, (480, 376))
        self.launcher.entry.set_text("No matching app " * 100)
        self.assertEqual(self.size(), expected)
        for mode in ("shell", "ai"):
            self.launcher.set_mode(mode)
            self.assertEqual(self.size(), expected, mode)
        self.launcher.settings()
        self.assertEqual(self.size(), expected)

    def test_follow_up_retains_answers_and_outputs_from_multiple_commands(self):
        launcher = self.launcher
        launcher.set_mode("ai")
        expected = self.size()
        with patch("luch.ui.threading.Thread"):
            launcher.ask("Сколько памяти и места на диске?")
        launcher.ai_event({"type": "text", "text": "Проверю память."})
        first_answer = launcher.answer_label
        launcher.ai_event({"type": "command", "command": "free -h"})
        launcher.ai_event({"type": "done", "awaiting_command": True})
        first_command_area = launcher.command_area
        with patch.object(launcher, "shell_thread"):
            launcher.approve_command()
        launcher.command_result(SimpleNamespace(output="Mem: available 16Gi", returncode=0))
        launcher.ai_event({"type": "text", "text": "Доступно 16 GiB. Проверю диск."})
        memory_answer = launcher.answer_label
        launcher.ai_event({"type": "command", "command": "df -h"})
        launcher.ai_event({"type": "done", "awaiting_command": True})
        second_command_area = launcher.command_area
        with patch.object(launcher, "shell_thread"):
            launcher.approve_command()
        launcher.command_result(SimpleNamespace(output="/dev/root: available 100G", returncode=0))
        launcher.ai_event({"type": "text", "text": "Свободно 100 G."})
        disk_answer = launcher.answer_label
        launcher.ai_event({"type": "done"})
        with patch("luch.ui.threading.Thread"):
            launcher.ask("А сколько занято?")
        launcher.ai_event({"type": "text", "text": "Занято 8 GiB памяти."})

        self.assertEqual(first_answer.get_text(), "Проверю память.")
        self.assertEqual(memory_answer.get_text(), "Доступно 16 GiB. Проверю диск.")
        self.assertEqual(disk_answer.get_text(), "Свободно 100 G.")
        self.assertEqual([child.get_text() for child in first_command_area.get_children()],
                         ["$ free -h", "Mem: available 16Gi"])
        self.assertEqual([child.get_text() for child in second_command_area.get_children()],
                         ["$ df -h", "/dev/root: available 100G"])
        questions = [child.get_text() for child in launcher.answer_body.get_children()
                     if isinstance(child, Gtk.Label)]
        self.assertIn("Сколько памяти и места на диске?", questions)
        self.assertIn("А сколько занято?", questions)
        self.assertEqual(launcher.answer_label.get_text(), "Занято 8 GiB памяти.")
        self.assertEqual(self.size(), expected)

    def test_long_dialogue_scrolls_to_follow_up_without_resizing(self):
        launcher = self.launcher
        launcher.set_mode("ai")
        expected = self.size()
        with patch("luch.ui.threading.Thread"):
            launcher.ask("Первый вопрос")
        previous_answer = launcher.answer_label
        launcher.ai_event({"type": "text", "text": "Строка предыдущего ответа\n" * 30})
        launcher.ai_event({"type": "done"})
        self.settle()
        with patch("luch.ui.threading.Thread"):
            launcher.ask("Уточнение")
        self.assertEqual(self.size(), expected)
        self.assertEqual(previous_answer.get_text(), "Строка предыдущего ответа\n" * 30)
        adjustment = launcher.content_scroller.get_vadjustment()
        self.assertGreater(adjustment.get_value(), 0)
        self.assertAlmostEqual(adjustment.get_value(), adjustment.get_upper() - adjustment.get_page_size(), delta=1)

    def test_declining_a_long_command_keeps_fixed_geometry(self):
        launcher = self.launcher
        launcher.set_mode("ai")
        expected = self.size()
        with patch("luch.ui.threading.Thread"):
            launcher.ask("Предложи команду")
        command = "echo " + "long-unbroken-argument" * 150
        launcher.ai_event({"type": "command", "command": command})
        launcher.ai_event({"type": "done", "awaiting_command": True})
        launcher.deny_command()
        self.assertEqual(self.size(), expected)
        self.assertEqual(launcher.command_area.get_children()[0].get_text(), f"$ {command}\nКоманда отклонена")


if __name__ == "__main__":
    unittest.main()
