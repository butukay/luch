"""Freedesktop application catalogue and small, local launch history."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from typing import Iterable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402


@dataclass(frozen=True)
class AppEntry:
    id: str
    name: str
    description: str = ""
    executable: str = ""
    icon: object | None = None
    keywords: str = ""
    categories: str = ""
    original_name: str = ""
    aliases: tuple[str, ...] = ()
    subtitle: str = ""
    pinned: bool = False
    command: str = ""
    cwd: str = ""
    terminal: bool | None = None
    icon_override: str = ""


_KEYBOARD = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.",
    "йцукенгшщзхъфывапролджэячсмитьбю",
)
_REVERSE_KEYBOARD = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.",
)
_ALIASES = {
    "chrome": "браузер хром интернет browser",
    "firefox": "браузер фаерфокс интернет browser",
    "thunar": "файлы файловый менеджер files folders папки",
    "nautilus": "файлы файловый менеджер files folders папки",
    "kitty": "терминал terminal консоль shell",
    "alacritty": "терминал terminal консоль shell",
    "foot": "терминал terminal консоль shell",
    "discord": "дискорд чат chat",
    "telegram": "телеграм телега чат chat",
    "steam": "стим игры games",
    "spotify": "спотифай музыка music",
    "codex": "кодекс код code",
    "chatgpt": "чатгпт ии assistant",
    "nvim": "неовим редактор editor код code",
    "code": "редактор editor код вскод vscode",
    "pavucontrol": "звук громкость volume sound",
    "htop": "процессы монитор system processes",
}
_FAVORITES = (
    "google-chrome.desktop", "kitty.desktop", "thunar.desktop",
    "discord.desktop", "org.telegram.desktop.desktop", "spotify.desktop",
    "steam.desktop", "chatgpt.desktop",
)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def _load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _normal(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _token_score(needle: str, text: str) -> float | None:
    """Prefer word prefixes, then substrings, then tightly grouped subsequences."""
    if not needle:
        return 0.0
    position = text.find(needle)
    if position >= 0:
        boundary = position == 0 or not text[position - 1].isalnum()
        return 90 + (30 if boundary else 0) + (30 if needle == text else 0) - position * 0.15
    cursor = 0
    positions = []
    for character in needle:
        position = text.find(character, cursor)
        if position < 0:
            return None
        positions.append(position)
        cursor = position + 1
    spread = positions[-1] - positions[0] + 1
    # Avoid noisy matches scattered through long descriptions.
    if len(needle) < 2 or spread > max(12, len(needle) * 3):
        return None
    return 40 - (spread - len(needle)) * 2 - positions[0] * 0.1


def match_score(entry: AppEntry, query: str) -> float | None:
    name = _normal(entry.name)
    app_id = _normal(entry.id.removesuffix(".desktop"))
    executable = _normal(Path(entry.executable).name)
    aliases = " ".join(value for key, value in _ALIASES.items() if key in app_id or key == executable)
    primary_aliases = " ".join(value for key, value in _ALIASES.items()
                               if key in app_id.split(".") or app_id.endswith("-" + key))
    fields = ((name, 1.2), (_normal(entry.original_name), 1.1), (app_id, 1.0), (executable, 1.0),
              (_normal(primary_aliases), 0.95),
              (_normal(entry.keywords + " " + aliases), 0.75),
              (_normal(entry.description + " " + entry.categories + " " + entry.subtitle), 0.5))
    fields += tuple((_normal(alias), 1.4) for alias in entry.aliases)
    original = _normal(query.strip())
    variants = dict.fromkeys((original, original.translate(_KEYBOARD), original.translate(_REVERSE_KEYBOARD)))
    best = None
    for variant in variants:
        total = 0.0
        for token in variant.split():
            scores = [score * weight for field, weight in fields
                      if (score := _token_score(token, field)) is not None]
            if not scores:
                break
            total += max(scores)
        else:
            best = max(total, best if best is not None else total)
    return best


class Catalog:
    def __init__(self, config: dict | None = None, *, state_path: Path | None = None):
        self.config = config or {}
        apps = self.config.get("apps", {})
        self.hidden = set(apps.get("hidden", self.config.get("hidden_apps", [])))
        self.favorites = tuple(apps.get("favorites", _FAVORITES))
        state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.state_path = state_path or state_dir / "luch/apps.json"
        loaded = _load_json(self.state_path, {})
        self._history = loaded if isinstance(loaded, dict) else {}
        self._entries: list[AppEntry] = []
        self._infos: dict[str, Gio.DesktopAppInfo] = {}
        self.reload()

    def reload(self) -> None:
        entries, infos = {}, {}
        apps = self.config.get("apps", {})
        profiles = apps.get("overrides", {})
        self.hidden = set(apps.get("hidden", self.config.get("hidden_apps", [])))
        # GIO resolves XDG_DATA_HOME/DIRS precedence, including Hidden overrides.
        for info in Gio.AppInfo.get_all():
            if not info.should_show():
                continue
            app_id = info.get_id()
            if not app_id or app_id in entries:
                continue
            keywords = getattr(info, "get_keywords", lambda: [])() or []
            categories = getattr(info, "get_categories", lambda: "")() or ""
            profile = profiles.get(app_id, {})
            original_name = info.get_display_name() or info.get_name() or app_id
            icon_override = profile.get("icon", "").strip()
            icon = info.get_icon()
            if icon_override:
                if "/" in icon_override or icon_override.startswith("~"):
                    path = Path(icon_override).expanduser()
                    if not path.is_absolute(): path = Path.home() / path
                    icon = Gio.FileIcon.new(Gio.File.new_for_path(str(path)))
                else:
                    icon = Gio.ThemedIcon.new(icon_override)
            if "hidden" in profile:
                if profile["hidden"]: self.hidden.add(app_id)
                else: self.hidden.discard(app_id)
            entries[app_id] = AppEntry(
                id=app_id, name=profile.get("name", "").strip() or original_name,
                description=info.get_description() or "",
                executable=info.get_executable() or "", icon=icon,
                keywords=" ".join(keywords), categories=categories,
                original_name=original_name, aliases=tuple(profile.get("aliases", [])),
                subtitle=profile.get("subtitle", ""), pinned=profile.get("pinned", False),
                command=profile.get("command", ""), cwd=profile.get("cwd", ""),
                terminal=profile.get("terminal"), icon_override=icon_override,
            )
            infos[app_id] = info
        self._entries = sorted(entries.values(), key=lambda entry: entry.name.casefold())
        self._infos = infos

    def set_hidden(self, ids: Iterable[str]) -> None:
        self.hidden = set(ids)

    def all(self, include_hidden: bool = False) -> list[AppEntry]:
        return [entry for entry in self._entries if include_hidden or entry.id not in self.hidden]

    def _usage(self, app_id: str) -> tuple[float, float]:
        value = self._history.get(app_id, {})
        if not isinstance(value, dict):
            return 0, 0
        try:
            return float(value.get("count", 0)), float(value.get("last", 0))
        except (TypeError, ValueError):
            return 0, 0

    def search(self, query: str = "", limit: int = 8, hidden: Iterable[str] | None = None) -> list[AppEntry]:
        hidden_ids = self.hidden if hidden is None else set(hidden)
        entries = [entry for entry in self._entries if entry.id not in hidden_ids]
        if not query.strip():
            favorites = {app_id: len(self.favorites) - index for index, app_id in enumerate(self.favorites)}
            return sorted(entries, key=lambda entry: (
                -entry.pinned, -self._usage(entry.id)[1], -favorites.get(entry.id, 0), entry.name.casefold()
            ))[:max(0, limit)]
        matches = []
        for entry in entries:
            score = match_score(entry, query)
            if score is not None:
                count, _ = self._usage(entry.id)
                matches.append((score + math.log1p(max(0, count)) * 2, entry))
        matches.sort(key=lambda item: (-item[0], -item[1].pinned, item[1].name.casefold()))
        return [entry for _, entry in matches[:max(0, limit)]]

    def launch(self, entry: AppEntry, launch_context=None) -> None:
        info = self._infos.get(entry.id)
        if info is None:
            raise RuntimeError("Приложение больше не установлено")
        native_terminal = getattr(info, "get_boolean", lambda _: False)("Terminal")
        if entry.command or entry.cwd or entry.terminal is not None:
            argv = shlex.split(entry.command) if entry.command.strip() else self._desktop_argv(info)
            terminal = native_terminal if entry.terminal is None else entry.terminal
            cwd = str(Path(entry.cwd).expanduser()) if entry.cwd else info.get_string("Path") or str(Path.home())
            self._spawn(argv, cwd, terminal)
        elif native_terminal:
            self._launch_terminal(info)
        elif not info.launch([], launch_context):
            raise RuntimeError(f"Не удалось запустить {entry.name}")
        count, _ = self._usage(entry.id)
        self._history[entry.id] = {"count": int(count) + 1, "last": time.time()}
        try:
            _atomic_json(self.state_path, self._history)
        except OSError:
            pass  # A read-only history directory must not prevent launching.

    def _desktop_argv(self, info):
        argv = shlex.split(info.get_string("Exec") or info.get_executable())
        expanded = []
        for argument in argv:
            if argument == "%i":
                icon = info.get_string("Icon")
                if icon:
                    expanded.extend(("--icon", icon))
                continue
            argument = argument.replace("%%", "\0").replace("%c", info.get_name())
            argument = argument.replace("%k", info.get_filename() or "")
            argument = re.sub(r"%[fFuUdDnNvm]", "", argument).replace("\0", "%")
            if argument:
                expanded.append(argument)
        return expanded

    def _spawn(self, argv, cwd, terminal=False):
        if not argv:
            raise RuntimeError("Не настроена команда запуска")
        argv = list(argv)
        argv[0] = os.path.expanduser(argv[0])
        if terminal:
            terminal_command = self.config.get("shell", {}).get("terminal", "kitty")
            terminal_args = shlex.split(terminal_command) if isinstance(terminal_command, str) else list(terminal_command)
            if not terminal_args:
                raise RuntimeError("Не настроена команда терминала")
            argv = terminal_args + ["-e"] + argv
        process = subprocess.Popen(argv, cwd=cwd, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import threading
        threading.Thread(target=process.wait, daemon=True).start()

    def _launch_terminal(self, info) -> None:
        expanded = self._desktop_argv(info)
        if not expanded:
            raise RuntimeError("Не настроена команда терминала")
        path = info.get_string("Path") or str(Path.home())
        self._spawn(expanded, path, terminal=True)
