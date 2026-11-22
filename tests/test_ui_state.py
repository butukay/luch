"""UI state regressions without constructing widgets or operating the desktop."""

import copy
import threading
import unittest
from unittest.mock import Mock, call, patch

from luch.config import DEFAULTS
from luch.ui import Launcher, MODES


class Entry:
    """Only the entry's public value matters to these state transitions."""
    def __init__(self, text=""):
        self.text = text
        self.grab_focus = Mock()
        self.set_position = Mock()
        self.set_sensitive = Mock()

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


def launcher_state(mode="apps"):
    launcher = Launcher.__new__(Launcher)
    launcher.config = Mock(data=copy.deepcopy(DEFAULTS), errors=[])
    launcher.catalog = Mock()
    launcher.client = Mock()
    launcher.mode = mode
    launcher.visible = True
    launcher.view = "main"
    launcher.queries = dict.fromkeys(MODES, "")
    launcher.entry = Entry()
    launcher.placeholder = Mock()
    launcher.leading_icon = Mock()
    launcher.mode_buttons = {mode: Mock() for mode in MODES}
    launcher.window = Mock()
    launcher.hints = Mock()
    launcher.save_button = Mock()
    launcher.mode_hint = Mock()
    launcher.history = Mock()
    launcher.command_area = Mock()
    launcher.command_area.get_children.return_value = []
    launcher.answer_label = Mock()
    launcher.answer_text = ""
    launcher.scroll_answer = Mock()
    launcher.generation = 12
    launcher.cancel_event = threading.Event()
    launcher.busy = False
    launcher.pending_command = None
    launcher.results = []
    launcher.selected = 0
    launcher.listbox = None
    launcher.render = Mock()
    launcher.render_hidden = Mock()
    launcher.apply_theme = Mock()
    launcher.choose_monitor = Mock()
    return launcher


class UiStateTest(unittest.TestCase):
    def test_show_resets_stale_input_and_queries_for_every_mode(self):
        for previous_mode in MODES:
            for requested_mode in MODES:
                with self.subTest(previous=previous_mode, requested=requested_mode):
                    launcher = launcher_state(previous_mode)
                    launcher.visible = False
                    launcher.entry.set_text("old visible input")
                    launcher.queries = {mode: f"old {mode} query" for mode in MODES}
                    with patch("luch.ui.Catalog"), patch("luch.ui.AiClient"):
                        launcher.show(requested_mode)
                    self.assertEqual(launcher.mode, requested_mode)
                    self.assertEqual(launcher.entry.get_text(), "")
                    self.assertEqual(launcher.queries, dict.fromkeys(MODES, ""))
                    self.assertEqual(launcher.view, "main")
                    self.assertTrue(launcher.visible)
                    launcher.window.show_all.assert_called_once()
                    launcher.placeholder.set_visible.assert_called_once_with(True)

    def test_switching_modes_retains_each_query_during_one_open_session(self):
        launcher = launcher_state()
        launcher.entry.set_text("chrome")
        launcher.set_mode("shell")
        launcher.entry.set_text("pwd")
        launcher.set_mode("ai")
        launcher.entry.set_text("Сколько памяти?")
        launcher.set_mode("apps")
        self.assertEqual(launcher.entry.get_text(), "chrome")
        launcher.set_mode("shell")
        self.assertEqual(launcher.entry.get_text(), "pwd")
        launcher.set_mode("ai")
        self.assertEqual(launcher.entry.get_text(), "Сколько памяти?")

    def test_reload_invalidates_pending_work_and_recreates_client_from_new_config(self):
        launcher = launcher_state("ai")
        launcher.busy = True
        launcher.view = "answer"
        launcher.pending_command = "df -h"
        old_client, old_cancel, generation = launcher.client, launcher.cancel_event, launcher.generation
        new_data = copy.deepcopy(DEFAULTS)
        new_data["ai"]["model"] = "custom:small"
        launcher.config.reload.side_effect = lambda: setattr(launcher.config, "data", new_data)
        queued, callback = [], Mock()
        with patch("luch.ui.GLib.idle_add", side_effect=queued.append), \
                patch("luch.ui.AiClient") as client_type, patch("luch.ui.Catalog") as catalog_type:
            launcher.post(generation, callback, "obsolete answer")
            launcher.dispatch("reload")
            client_type.assert_called_once_with(new_data)
            catalog_type.assert_called_once_with(new_data)
            self.assertIs(launcher.client, client_type.return_value)
        self.assertTrue(old_cancel.is_set())
        old_client.cancel.assert_called_once()
        self.assertGreater(launcher.generation, generation)
        self.assertIsNone(launcher.pending_command)
        self.assertFalse(launcher.busy)
        self.assertEqual(launcher.view, "main")
        self.assertFalse(queued[0]())
        callback.assert_not_called()

    def test_hidden_manager_invalidates_active_worker_before_changing_view(self):
        launcher = launcher_state("ai")
        launcher.entry.set_text("question in flight")
        launcher.busy = True
        launcher.pending_command = "wpctl get-volume @DEFAULT_AUDIO_SINK@"
        generation, cancel = launcher.generation, launcher.cancel_event
        queued, callback = [], Mock()
        with patch("luch.ui.GLib.idle_add", side_effect=queued.append):
            launcher.post(generation, callback)
            launcher.hidden_view()
        self.assertTrue(cancel.is_set())
        launcher.client.cancel.assert_called_once()
        self.assertGreater(launcher.generation, generation)
        self.assertFalse(launcher.busy)
        self.assertIsNone(launcher.pending_command)
        self.assertEqual(launcher.entry.get_text(), "")
        launcher.render_hidden.assert_called_once()
        self.assertFalse(queued[0]())
        callback.assert_not_called()

    def test_deny_clears_busy_and_discards_already_queued_ai_events(self):
        launcher = launcher_state("ai")
        launcher.busy = True  # The proposal can arrive just before the done event.
        launcher.pending_command = "touch ~/example"
        generation, cancel = launcher.generation, launcher.cancel_event
        queued, callback = [], Mock()
        with patch("luch.ui.GLib.idle_add", side_effect=queued.append), patch("luch.ui.label"):
            launcher.post(generation, callback, {"type": "command", "command": "obsolete"})
            launcher.deny_command()
        self.assertTrue(cancel.is_set())
        self.assertFalse(launcher.busy)
        self.assertIsNone(launcher.pending_command)
        launcher.client.cancel.assert_called_once()
        launcher.entry.grab_focus.assert_called_once()
        self.assertFalse(queued[0]())
        callback.assert_not_called()

    def test_callbacks_only_update_visible_current_generation(self):
        launcher = launcher_state()
        queued, callback = [], Mock()
        with patch("luch.ui.GLib.idle_add", side_effect=queued.append):
            launcher.post(launcher.generation, callback, "current")
            launcher.post(launcher.generation - 1, callback, "old")
        self.assertFalse(queued[0]())
        self.assertFalse(queued[1]())
        callback.assert_called_once_with("current")
        callback.reset_mock()
        launcher.visible = False
        self.assertFalse(queued[0]())
        callback.assert_not_called()

    def test_follow_up_appends_to_existing_answer_and_restarts_worker(self):
        launcher = launcher_state("ai")
        launcher.view = "answer"
        launcher.answer_text = "Предыдущий ответ"
        launcher.answer_view = Mock()
        launcher.append_answer = Mock()
        previous_label = launcher.answer_label
        previous_cancel = launcher.cancel_event
        launcher.entry.set_text("А сколько занято?")
        with patch("luch.ui.threading.Thread") as worker:
            launcher.ask(launcher.entry.get_text())
        launcher.answer_view.assert_not_called()
        launcher.append_answer.assert_called_once_with("А сколько занято?", scroll=True)
        previous_label.set_text.assert_not_called()
        self.assertTrue(previous_cancel.is_set())
        self.assertTrue(launcher.busy)
        self.assertFalse(launcher.cancel_event.is_set())
        self.assertEqual(launcher.entry.get_text(), "")
        worker.return_value.start.assert_called_once()

    def test_new_message_retires_pending_confirmation_before_appending(self):
        launcher = launcher_state("ai")
        launcher.view = "answer"
        launcher.pending_command = "touch ~/example"
        old_card = Mock()
        launcher.command_area.get_children.return_value = [old_card]
        launcher.answer_view = Mock()
        launcher.append_answer = Mock()
        with patch("luch.ui.threading.Thread"), patch("luch.ui.label") as label:
            launcher.ask("Сначала объясни, зачем эта команда")
        old_card.destroy.assert_called_once()
        label.assert_called_once_with("$ touch ~/example\nКоманда отклонена", "mono")
        self.assertIsNone(launcher.pending_command)
        launcher.append_answer.assert_called_once_with("Сначала объясни, зачем эта команда", scroll=True)
        launcher.answer_view.assert_not_called()

    def test_error_allows_a_follow_up_without_clearing_previous_view(self):
        launcher = launcher_state("ai")
        launcher.view = "answer"
        launcher.busy = True
        launcher.answer_view = Mock()
        launcher.append_answer = Mock()
        launcher.ai_event({"type": "error", "message": "Нет связи"})
        self.assertFalse(launcher.busy)
        launcher.answer_label.set_text.assert_called_once_with("Нет связи")
        with patch("luch.ui.threading.Thread"):
            launcher.ask("Попробуй ещё раз")
        launcher.append_answer.assert_called_once_with("Попробуй ещё раз", scroll=True)
        launcher.answer_view.assert_not_called()

    def test_history_command_can_be_repeated_after_result_list_is_cleared(self):
        launcher = launcher_state("shell")
        launcher.results = ["pwd", "ls -la"]
        launcher.selected = 1

        def answer_view(*_):
            launcher.view = "answer"
            launcher.results = []

        launcher.answer_view = Mock(side_effect=answer_view)
        # No worker is started, and no shell subprocess is created by this test.
        with patch("luch.ui.threading.Thread") as worker:
            launcher.activate()
            self.assertEqual(launcher.last_shell_command, "ls -la")
            self.assertEqual(launcher.results, [])
            self.assertEqual(launcher.entry.get_text(), "")
            launcher.busy = False
            launcher.activate()
        self.assertEqual(worker.call_count, 2)
        launcher.history.add.assert_has_calls([call("ls -la"), call("ls -la")])
        self.assertEqual(launcher.answer_view.call_args_list,
                         [call("$ ls -la", "SHELL"), call("$ ls -la", "SHELL")])

    def test_empty_main_shell_view_does_not_replay_previous_session(self):
        launcher = launcher_state("shell")
        launcher.last_shell_command = "old command"
        with patch("luch.ui.threading.Thread") as worker:
            launcher.activate()
        worker.assert_not_called()
        launcher.history.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
