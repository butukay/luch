"""Bounded shell execution, command history, and PATH completion."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time

from .catalog import _atomic_json, _load_json


@dataclass(frozen=True)
class ShellResult:
    command: str
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False
    cancelled: bool = False

    @property
    def output(self) -> str:
        return "\n".join(part.rstrip() for part in (self.stdout, self.stderr) if part.strip())


def _stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    # The group leader can exit while a child ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command: str, cwd: str | Path | None = None, timeout: float = 20,
                cancel: threading.Event | None = None, max_output: int = 32768,
                shell: str | None = None) -> ShellResult:
    """Run explicitly submitted shell text; keep only the tail of large output.

    Cancellation and timeout terminate the complete process group. Both output
    pipes are drained continuously, avoiding deadlocks and unbounded buffering.
    """
    if cancel is not None and cancel.is_set():
        return ShellResult(command, "", "", -signal.SIGTERM, cancelled=True)
    max_output = max(1, int(max_output))
    interpreter = shell or os.environ.get("SHELL") or "/bin/sh"
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [interpreter, "-c", command], cwd=Path(cwd).expanduser() if cwd else Path.home(),
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, ValueError) as error:
        return ShellResult(command, "", str(error), 127)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    timed_out = cancelled = False
    selector = selectors.DefaultSelector()
    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    try:
        while selector.get_map():
            cancelled = cancel is not None and cancel.is_set()
            timed_out = time.monotonic() - started >= timeout
            if cancelled or timed_out:
                _stop_group(process)
                break
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer = buffers[key.data]
                buffer.extend(chunk)
                if len(buffer) > max_output:
                    del buffer[:-max_output]
                    truncated[key.data] = True
        if process.poll() is None:
            # A process may close its output early and continue running.
            while process.poll() is None:
                cancelled = cancel is not None and cancel.is_set()
                timed_out = time.monotonic() - started >= timeout
                if cancelled or timed_out:
                    _stop_group(process)
                    break
                time.sleep(0.03)
        process.wait()
    finally:
        selector.close()
        for pipe in (process.stdout, process.stderr):
            if pipe and not pipe.closed:
                pipe.close()
        if process.poll() is None:
            _stop_group(process)
    outputs = {
        name: ("… начало вывода пропущено …\n" if truncated[name] else "") + value.decode("utf-8", errors="replace")
        for name, value in buffers.items()
    }
    return ShellResult(command, outputs["stdout"], outputs["stderr"], process.returncode,
                       timed_out=timed_out, cancelled=cancelled)


_EXEC_CACHE: tuple[str, float, list[str]] = ("", 0, [])


def executable_matches(query: str, limit: int = 6) -> list[str]:
    global _EXEC_CACHE
    query = query.strip()
    if not query or any(character.isspace() for character in query) or "/" in query:
        return []
    path = os.environ.get("PATH", "")
    if path != _EXEC_CACHE[0] or time.monotonic() - _EXEC_CACHE[1] > 30:
        names = set()
        for directory in dict.fromkeys(path.split(os.pathsep)):
            if not directory:
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_file() and os.access(entry.path, os.X_OK):
                            names.add(entry.name)
            except OSError:
                continue
        _EXEC_CACHE = (path, time.monotonic(), sorted(names))
    folded = query.casefold()
    return [name for name in _EXEC_CACHE[2] if name.casefold().startswith(folded)][:max(0, limit)]


class ShellHistory:
    def __init__(self, path: Path | None = None, max_items: int = 100):
        state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.path = path or state_dir / "luch/shell.json"
        self.max_items = max_items
        saved = _load_json(self.path, [])
        self._items = [item for item in saved if isinstance(item, str)] if isinstance(saved, list) else []

    def items(self, query: str = "", limit: int = 6) -> list[str]:
        needle = query.casefold().strip()
        return [item for item in self._items if needle in item.casefold()][:max(0, limit)]

    def add(self, command: str) -> None:
        # A leading space provides the familiar HISTCONTROL=ignorespace opt-out.
        if not command.strip() or command.startswith(" "):
            return
        self._items = [command] + [item for item in self._items if item != command]
        self._items = self._items[:self.max_items]
        try:
            _atomic_json(self.path, self._items)
        except OSError:
            pass
