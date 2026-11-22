"""A tiny command client. GTK is imported only by the resident daemon."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from . import __version__
from .config import Config


def runtime_dir():
    directory = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/luch-{os.getuid()}")) / "luch"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Небезопасный каталог: {directory}")
    directory.chmod(0o700)
    return directory


def request(command, timeout=3):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(runtime_dir() / "control.sock"))
        connection.sendall(json.dumps({"command": command}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n") and len(data) < 65536:
            part = connection.recv(4096)
            if not part: break
            data += part
        return json.loads(data)


def daemon():
    directory = runtime_dir()
    lock = (directory / "daemon.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    # Keep lock alive throughout Gtk.main().
    lock.write(str(os.getpid())); lock.flush()
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        print("Луч: нет Wayland/X11-сессии", file=sys.stderr)
        return 1
    from gi.repository import GLib
    from . import ui
    path = directory / "control.sock"
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(8)

    def cleanup():
        server.close()
        path.unlink(missing_ok=True)

    def ready(launcher):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: launcher.dispatch("quit"))
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: launcher.dispatch("quit"))

        def serve():
            while True:
                try: connection, _ = server.accept()
                except OSError: return
                with connection:
                    try:
                        connection.settimeout(2)
                        data = b""
                        while not data.endswith(b"\n") and len(data) < 4096:
                            part = connection.recv(4096)
                            if not part: break
                            data += part
                        payload = json.loads(data)
                        if not isinstance(payload, dict):
                            connection.sendall(b'{"error":"expected object"}\n'); continue
                        command = payload.get("command")
                        if command not in ("toggle", "apps", "shell", "ai", "quit", "hide", "reload", "settings", "status"):
                            connection.sendall(b'{"error":"unknown command"}\n'); continue
                        finished = threading.Event()
                        result = {}
                        def dispatch(command=command, result=result, finished=finished):
                            try:
                                if command != "status": launcher.dispatch(command)
                                result.update(launcher.status())
                            except Exception as error:
                                import traceback
                                traceback.print_exc()
                                result["error"] = str(error)
                            finally: finished.set()
                            return False
                        GLib.idle_add(dispatch)
                        if not finished.wait(2): result["error"] = "UI busy"
                        connection.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                    except (OSError, ValueError):
                        pass
        threading.Thread(target=serve, name="luch-control", daemon=True).start()
    try:
        ui.run(ready)
    finally:
        cleanup()
        lock.close()
    return 0


def ensure_running():
    executable = Path(__file__).resolve().parent.parent / "bin/luch"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home()/".local/state")) / "luch"
    state.mkdir(parents=True, exist_ok=True)
    unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home()/".config")) / "systemd/user/luch.service"
    if unit.exists():
        try:
            service = subprocess.run(["systemctl", "--user", "start", "luch.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            if service.returncode == 0:
                for _ in range(20):
                    try:
                        request("status", timeout=.2)
                        return
                    except (OSError, ValueError): time.sleep(.05)
        except (OSError, subprocess.TimeoutExpired):
            pass
    with (state / "luch.log").open("a") as log:
        subprocess.Popen([sys.executable, str(executable), "daemon"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(60):
        try:
            request("status", timeout=.2)
            return
        except (OSError, ValueError): time.sleep(.05)
    raise RuntimeError(f"Не удалось запустить Луч. Журнал: {state/'luch.log'}")


def doctor():
    import importlib.util
    import urllib.request
    result = {"version": __version__, "config": str(Config().path), "session": os.environ.get("XDG_SESSION_TYPE"), "display": os.environ.get("WAYLAND_DISPLAY", ""), "pygobject": bool(importlib.util.find_spec("gi"))}
    try: result["launcher"] = request("status")
    except (OSError, ValueError): result["launcher"] = "stopped"
    config = Config()
    result["config_errors"] = config.errors
    from .ai import AiClient

    result["ai"] = AiClient(config.data).check_connection()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="luch", description="Луч · минималистичный Wayland-лаунчер")
    parser.add_argument("command", nargs="?", default="toggle", choices=["toggle", "daemon", "apps", "shell", "ai", "quit", "close", "status", "reload", "settings", "list", "hide", "unhide", "config", "doctor"])
    parser.add_argument("app_id", nargs="?")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        if args.command == "daemon": return daemon()
        if args.command == "doctor": return doctor()
        if args.command in ("hide", "unhide"):
            if not args.app_id: parser.error("нужен desktop ID приложения: luch list")
            config = Config()
            getattr(config, args.command)(args.app_id)
            try: request("reload")
            except (OSError, ValueError): pass
            return 0
        if args.command == "list":
            from .catalog import Catalog
            config = Config()
            catalog = Catalog(config.data)
            for app in catalog.all(include_hidden=True):
                hidden = " [скрыто]" if app.id in catalog.hidden else ""
                print(f"{app.id}\t{app.name}{hidden}")
            return 0
        if args.command == "config":
            config = Config()
            if not config.path.exists(): config.save()
            print(config.path)
            return 0
        command = "hide" if args.command == "close" else args.command
        try: result = request(command)
        except (OSError, ValueError):
            if command in ("quit", "hide"): return 0
            if command == "status":
                print('{"running": false}')
                return 1
            ensure_running()
            result = request(command)
        if command == "status" or "error" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if "error" in result else 0
    except (RuntimeError, OSError) as error:
        print(f"Луч: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
