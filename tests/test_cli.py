"""Exercise actual Unix IPC and daemon locking without touching the desktop.

Each test launches cli.daemon in its own runtime directory. Only the GTK
main-loop adapter and launcher UI are replaced by an in-process queue stub.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest


DAEMON = r'''
import os, queue, sys, time, types
jobs = queue.Queue()
class GLib:
    PRIORITY_DEFAULT = 0
    @staticmethod
    def idle_add(callback):
        jobs.put(callback)
    @staticmethod
    def unix_signal_add(*args):
        return 1
repository = types.ModuleType("gi.repository")
repository.GLib = GLib
gi = types.ModuleType("gi")
gi.repository = repository
sys.modules["gi"] = gi
sys.modules["gi.repository"] = repository
class Launcher:
    def __init__(self):
        self.running = True
        self.visible = False
        self.mode = "apps"
        self.actions = []
    def dispatch(self, command):
        self.actions.append(command)
        if command == "quit": self.running = False
        elif command == "hide": self.visible = False
        elif command == "toggle":
            if self.visible:
                modes = ["apps", "shell", "ai"]
                self.mode = modes[(modes.index(self.mode) + 1) % 3]
            else:
                self.visible = True
                self.mode = "apps"
        elif command in ("apps", "shell", "ai"):
            self.visible = True
            self.mode = command
    def status(self):
        return {"running": self.running, "visible": self.visible, "mode": self.mode,
                "actions": list(self.actions)}
def run(ready):
    launcher = Launcher()
    ready(launcher)
    time.sleep(float(os.environ.get("LUCH_TEST_UI_DELAY", "0")))
    while launcher.running:
        try: jobs.get(timeout=.2)()
        except queue.Empty: pass
    time.sleep(.05)
ui = types.ModuleType("luch.ui")
ui.run = run
sys.modules["luch.ui"] = ui
from luch.cli import daemon
raise SystemExit(daemon())
'''


class CliIpcTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luch-ipc-test-")
        self.socket_path = Path(self.temp.name) / "luch/control.sock"
        self.environment = dict(os.environ, XDG_RUNTIME_DIR=self.temp.name,
                                WAYLAND_DISPLAY="test-only", LUCH_TEST_UI_DELAY="0")
        self.environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        self.process = None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            try:
                self.request("quit", timeout=.5)
            except (OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        self.temp.cleanup()

    def start(self, delay=0):
        self.environment["LUCH_TEST_UI_DELAY"] = str(delay)
        self.process = subprocess.Popen([sys.executable, "-c", DAEMON], env=self.environment,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                return
            self.assertIsNone(self.process.poll(), "isolated daemon exited during startup")
            time.sleep(.01)
        self.fail("isolated daemon did not create socket")

    def raw(self, payload, *, timeout=1):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(self.socket_path))
            connection.sendall(payload)
            data = b""
            while not data.endswith(b"\n"):
                part = connection.recv(4096)
                if not part:
                    break
                data += part
            return json.loads(data) if data else None

    def request(self, command, *, timeout=1):
        return self.raw(json.dumps({"command": command}).encode() + b"\n", timeout=timeout)

    def test_toggle_cycles_modes_and_close_preserves_daemon(self):
        self.start()
        self.assertFalse(self.request("status")["visible"])
        self.assertEqual("apps", self.request("toggle")["mode"])
        self.assertEqual("shell", self.request("toggle")["mode"])
        self.assertEqual("ai", self.request("toggle")["mode"])
        self.assertEqual("apps", self.request("toggle")["mode"])
        self.assertFalse(self.request("hide")["visible"])
        self.assertIsNone(self.process.poll())
        self.assertEqual("apps", self.request("toggle")["mode"])

    def test_malformed_payloads_do_not_kill_ipc_worker(self):
        self.start()
        for payload in [b"[]\n", b"null\n", b'"hello"\n', b"42\n", b"not json\n"]:
            with self.subTest(payload=payload):
                self.raw(payload)
                self.assertTrue(self.request("status")["running"])

    def test_unknown_command_does_not_change_launcher_state(self):
        self.start()
        result = self.request("not-an-action")
        self.assertIn("error", result)
        self.assertEqual([], self.request("status")["actions"])

    def test_duplicate_daemon_cannot_unlink_active_socket(self):
        self.start()
        duplicate = subprocess.run([sys.executable, "-c", DAEMON], env=self.environment,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        self.assertEqual(0, duplicate.returncode)
        self.assertTrue(self.request("status")["running"])
        self.assertIsNone(self.process.poll())

    def test_private_socket_permissions(self):
        self.start()
        self.assertEqual(0o700, stat.S_IMODE(self.socket_path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.socket_path.stat().st_mode))

    def test_concurrent_requests_are_serialized_without_lost_toggles(self):
        self.start()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request("toggle", timeout=3), range(20)))
        self.assertTrue(all("error" not in result for result in results))
        state = self.request("status")
        self.assertEqual(20, len(state["actions"]))
        self.assertEqual("shell", state["mode"])

    def test_dispatch_after_timeout_keeps_original_command_binding(self):
        self.start(delay=2.3)
        first = self.request("toggle", timeout=3)
        self.assertIn("error", first)
        second = self.request("ai", timeout=2)
        self.assertEqual(["toggle", "ai"], second["actions"])
        self.assertEqual("ai", second["mode"])

    def test_quit_releases_lock_and_removes_socket(self):
        self.start()
        self.request("quit")
        self.process.wait(timeout=2)
        self.assertEqual(0, self.process.returncode)
        self.assertFalse(self.socket_path.exists())
        self.start()
        self.assertTrue(self.request("status")["running"])


if __name__ == "__main__":
    unittest.main()
