import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from luch.shell import ShellHistory, executable_matches, run_command


class ShellTests(unittest.TestCase):
    def test_capture_both_streams_and_exit_code(self):
        result = run_command("printf 'hello'; printf 'problem' >&2; exit 7", shell="/bin/sh")
        self.assertEqual((result.stdout, result.stderr, result.returncode), ("hello", "problem", 7))
        self.assertEqual(result.output, "hello\nproblem")

    def test_cwd_and_user_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_command("pwd", cwd=directory, shell="/bin/bash")
        self.assertEqual(result.stdout.strip(), directory)

    def test_large_output_is_bounded_without_blocking(self):
        result = run_command("head -c 200000 /dev/zero; head -c 200000 /dev/zero >&2", max_output=1024)
        self.assertEqual(result.returncode, 0)
        self.assertLess(len(result.stdout), 1100)
        self.assertLess(len(result.stderr), 1100)
        self.assertIn("пропущено", result.stdout)

    def test_timeout_kills_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "child.pid"
            result = run_command(f"sleep 60 & echo $! > {pidfile}; wait", timeout=0.2, shell="/bin/sh")
            child = int(pidfile.read_text())
            self.assertTrue(result.timed_out)
            self.assertLess(result.returncode, 0)
            # A killed child may briefly remain as a zombie until init reaps it.
            stat = Path(f"/proc/{child}/stat")
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")

    def test_timeout_when_command_closes_its_output(self):
        start = time.monotonic()
        result = run_command("exec 1>&- 2>&-; sleep 60", timeout=0.15, shell="/bin/sh")
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - start, 2)

    def test_cancel_running_command(self):
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            result = run_command("sleep 60", cancel=cancel)
        finally:
            timer.cancel()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)

    def test_precancelled_does_not_start_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch("luch.shell.subprocess.Popen") as popen:
            result = run_command("true", cancel=cancel)
        self.assertTrue(result.cancelled)
        popen.assert_not_called()

    def test_missing_shell_is_reported(self):
        result = run_command("true", shell="/no/such/shell")
        self.assertEqual(result.returncode, 127)
        self.assertTrue(result.stderr)

    def test_history_deduplicates_filters_and_preserves_leading_space_optout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shell.json"
            history = ShellHistory(path)
            for command in ("ls -la", "pwd", "ls -la", " secret", "   "):
                history.add(command)
            self.assertEqual(history.items(), ["ls -la", "pwd"])
            self.assertEqual(history.items("LS"), ["ls -la"])
            self.assertEqual(ShellHistory(path).items(), ["ls -la", "pwd"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_path_completion_only_includes_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "luch-command"
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            (Path(directory) / "luch-document").write_text("text")
            with patch.dict(os.environ, {"PATH": directory}):
                self.assertEqual(executable_matches("luch"), ["luch-command"])
                self.assertEqual(executable_matches("luch arg"), [])


if __name__ == "__main__":
    unittest.main()
