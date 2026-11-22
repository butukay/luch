"""Human-editable settings, with conservative validation and atomic writes."""
from __future__ import annotations

import copy
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import re
import shlex
import tempfile
import tomllib
from urllib.parse import urlsplit

MODES = ("apps", "shell", "ai")

DEFAULTS = {
    "window": {
        "width": 700, "rows": 7, "radius": 20, "padding": 20,
        "position": "center", "top_margin": 240, "opacity": 1.0,
        "font": "Adwaita Sans", "font_size": 14, "row_height": 52,
        "monitor": "", "close_on_focus_loss": False,
    },
    "theme": {
        "background": "#14121b", "surface": "#1c1926", "selection": "#35303f",
        "text": "#ede9f5", "muted": "#a49caf", "accent": "#a78bfa",
        "border": "#454050", "success": "#a5c9ad", "error": "#ef9aa9",
    },
    "modes": {"apps": True, "shell": True, "ai": True, "on_last_toggle": "wrap"},
    "apps": {"hidden": [], "favorites": ["google-chrome.desktop", "kitty.desktop", "thunar.desktop", "discord.desktop", "org.telegram.desktop.desktop", "spotify.desktop", "steam.desktop", "chatgpt.desktop"], "icon_style": "outline", "overrides": {}},
    "shell": {"cwd": "~", "timeout": 30, "terminal": "kitty"},
    "ai": {
        "endpoint": "http://127.0.0.1:11434/v1", "model": "qwen3:4b-instruct-2507-q4_K_M",
        "api_format": "openai", "api_key": "",
        "keep_alive": -1, "num_ctx": 4096, "num_predict": 384,
        "temperature": 0.2, "timeout": 60,
    },
}

APP_DEFAULTS = {
    "name": "", "aliases": [], "subtitle": "", "hidden": False,
    "pinned": False, "command": "", "cwd": "", "terminal": None, "icon": "",
}


def desktop_id(value):
    return (isinstance(value, str) and value.endswith(".desktop") and len(value) <= 512
            and "/" not in value and "\\" not in value
            and not any(ord(character) < 32 for character in value))


def _validate_app_profiles(apps):
    errors = []
    profiles = apps.get("overrides", {})
    if not isinstance(profiles, dict):
        apps["overrides"] = {}
        return ["apps.overrides: нужна таблица настроек приложений"]
    for app_id, profile in list(profiles.items()):
        prefix = f"apps.overrides.{app_id}"
        if not desktop_id(app_id) or not isinstance(profile, dict):
            profiles.pop(app_id)
            errors.append(f"{prefix}: нужны desktop ID и таблица настроек")
            continue
        for key in ("name", "subtitle", "command", "cwd", "icon"):
            if key not in profile:
                continue
            value = profile[key]
            limit = 200 if key in ("name", "subtitle") else 4096
            if (not isinstance(value, str) or len(value) > limit
                    or any(ord(character) < 32 for character in value)):
                profile.pop(key)
                errors.append(f"{prefix}.{key}: нужна строка до {limit} символов без управляющих символов")
        for key in ("hidden", "pinned", "terminal"):
            if key in profile and not isinstance(profile[key], bool):
                profile.pop(key)
                errors.append(f"{prefix}.{key}: true или false")
        if "aliases" in profile:
            aliases = profile["aliases"]
            if (not isinstance(aliases, list) or len(aliases) > 64 or not all(
                    isinstance(alias, str) and 0 < len(alias.strip()) <= 200
                    and not any(ord(character) < 32 for character in alias) for alias in aliases)):
                profile.pop("aliases")
                errors.append(f"{prefix}.aliases: нужен список непустых поисковых алиасов")
            else:
                profile["aliases"] = list(dict.fromkeys(alias.strip() for alias in aliases))
        if profile.get("command", "").strip():
            try:
                if not shlex.split(profile["command"]):
                    raise ValueError("empty command")
            except ValueError:
                profile.pop("command")
                errors.append(f"{prefix}.command: исправь кавычки в команде запуска")
    return errors


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "luch/config.toml"


def _merge(base, overlay):
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def validate(data):
    """Repair the supported schema in place, retaining unrelated TOML values."""
    errors = []

    def reset(section, key, expected):
        errors.append(f"{section}.{key}: {expected}")
        data[section][key] = copy.deepcopy(DEFAULTS[section][key])

    def number(section, key, lo, hi, *, integer=False):
        value = data[section][key]
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if valid:
            try:
                valid = math.isfinite(value) and lo <= value <= hi
                if integer:
                    valid = valid and value == int(value)
            except (OverflowError, ValueError):
                valid = False
        if not valid:
            reset(section, key, f"допустимо {'целое число ' if integer else ''}{lo}–{hi}")
        elif integer:
            data[section][key] = int(value)

    def string(section, key, *, allow_empty=False, maximum=4096):
        value = data[section][key]
        if (not isinstance(value, str) or len(value) > maximum
                or any(ord(character) < 32 for character in value)
                or (not allow_empty and not value.strip())):
            reset(section, key, "нужна строка без управляющих символов")

    for section, defaults in DEFAULTS.items():
        if not isinstance(data.get(section), dict):
            errors.append(f"{section}: нужна TOML-секция [{section}]")
            data[section] = copy.deepcopy(defaults)
        else:
            for key, value in defaults.items():
                data[section].setdefault(key, copy.deepcopy(value))

    w = data["window"]
    for key, lo, hi in [("width", 440, 1400), ("rows", 3, 12), ("radius", 0, 40), ("padding", 12, 40), ("font_size", 11, 24), ("row_height", 40, 80), ("top_margin", 0, 1600)]:
        number("window", key, lo, hi, integer=True)
    number("window", "opacity", 0.5, 1)
    if w["position"] not in ("center", "top", "bottom"):
        reset("window", "position", "center, top или bottom")
    for key in ("font", "monitor"):
        string("window", key, allow_empty=key == "monitor", maximum=200)
    # Font is interpolated into GTK CSS. Never permit CSS syntax here.
    if not re.fullmatch(r"[\w ,.-]+", w["font"]):
        reset("window", "font", "нужно имя шрифта без синтаксиса CSS")
    if not isinstance(w["close_on_focus_loss"], bool):
        reset("window", "close_on_focus_loss", "true или false")
    modes = data["modes"]
    for mode in MODES:
        if not isinstance(modes[mode], bool):
            reset("modes", mode, "true или false")
    if not any(modes[mode] for mode in MODES):
        reset("modes", "apps", "нужно оставить хотя бы один раздел")
    if modes["on_last_toggle"] not in ("wrap", "close"):
        reset("modes", "on_last_toggle", "wrap или close")
    for key in DEFAULTS["theme"]:
        value = data["theme"][key]
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            reset("theme", key, "нужен цвет #RRGGBB")
    for key in ("hidden", "favorites"):
        values = data["apps"][key]
        if not isinstance(values, list) or not all(
                isinstance(value, str) and value.endswith(".desktop") and len(value) <= 512
                and "/" not in value and not any(ord(character) < 32 for character in value)
                for value in values):
            reset("apps", key, "нужен список desktop ID, например kitty.desktop")
        else:
            data["apps"][key] = list(dict.fromkeys(values))
    if data["apps"]["icon_style"] not in ("outline", "symbolic"):
        reset("apps", "icon_style", "outline или symbolic")
    errors.extend(_validate_app_profiles(data["apps"]))
    number("shell", "timeout", 1, 300)
    for key in ("cwd", "terminal"):
        string("shell", key)
    try:
        if not shlex.split(data["shell"]["terminal"]):
            raise ValueError("empty terminal")
    except ValueError:
        reset("shell", "terminal", "нужна команда терминала с корректными кавычками")

    ai = data["ai"]
    if ai["api_format"] not in ("openai", "ollama"):
        reset("ai", "api_format", "openai или ollama")
    string("ai", "api_key", allow_empty=True)
    string("ai", "endpoint", maximum=2048)
    try:
        endpoint = urlsplit(ai["endpoint"])
        valid = (endpoint.scheme in ("http", "https") and endpoint.hostname
                 and endpoint.username is None and endpoint.password is None
                 and not endpoint.query and not endpoint.fragment
                 and not any(character.isspace() for character in ai["endpoint"]))
        if endpoint.port is not None and not 1 <= endpoint.port <= 65535:
            valid = False
        if not valid:
            raise ValueError("invalid URL")
    except ValueError:
        reset("ai", "endpoint", "нужен URL http:// или https:// без логина, query и fragment")
    string("ai", "model", maximum=256)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", ai["model"]):
        reset("ai", "model", "нужно имя модели без пробелов")
    number("ai", "num_ctx", 512, 262144, integer=True)
    number("ai", "num_predict", 1, 8192, integer=True)
    number("ai", "temperature", 0, 2)
    number("ai", "timeout", 1, 300)
    keep_alive = ai["keep_alive"]
    if isinstance(keep_alive, str):
        if keep_alive == "-1":
            ai["keep_alive"] = -1
        else:
            valid = len(keep_alive) <= 100 and bool(re.fullmatch(
                r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+|0", keep_alive))
            if valid:
                factors = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}
                duration = sum(float(amount) * factors[unit] for amount, unit in re.findall(
                    r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)", keep_alive))
                valid = math.isfinite(duration) and duration <= 31536000
            if not valid:
                reset("ai", "keep_alive", "-1, секунды или длительность, например 30m или 24h")
    else:
        try:
            valid = (not isinstance(keep_alive, bool) and isinstance(keep_alive, (int, float))
                     and math.isfinite(keep_alive) and (keep_alive == -1 or 0 <= keep_alive <= 31536000))
        except OverflowError:
            valid = False
        if not valid:
            reset("ai", "keep_alive", "-1, секунды или длительность, например 30m или 24h")
    return errors


def _toml_value(value):
    """Serialize the value types tomllib returns, including unknown user keys."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items()) + " }"
    raise TypeError(f"Неподдерживаемое TOML-значение: {type(value).__name__}")


def _toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_path()
        self.data = copy.deepcopy(DEFAULTS)
        self.errors = []
        self.reload()

    def reload(self):
        self.data = copy.deepcopy(DEFAULTS)
        self.errors = []
        try:
            with self.path.open("rb") as stream:
                raw = tomllib.load(stream)
            if (isinstance(raw.get("ai"), dict) and "api_format" not in raw["ai"]
                    and isinstance(raw["ai"].get("endpoint"), str)
                    and raw["ai"]["endpoint"].startswith(("http://", "https://"))):
                raw["ai"]["api_format"] = "ollama"
            _merge(self.data, raw)
        except FileNotFoundError:
            pass
        except (OSError, tomllib.TOMLDecodeError) as error:
            self.errors.append(str(error))
        self.errors.extend(validate(self.data))
        return self.data

    def save(self):
        self.errors.extend(validate(self.data))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Настройки лаунчера", "# Сохрани файл — при следующем открытии тема применится автоматически.", ""]
        # Root scalar keys must appear before the first table header.
        for key, value in self.data.items():
            if not isinstance(value, dict):
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for section, values in self.data.items():
            if not isinstance(values, dict):
                continue
            lines.append(f"[{_toml_key(section)}]")
            for key, value in values.items():
                if section == "apps" and key == "overrides" and value:
                    continue
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
            lines.append("")
            if section == "apps":
                for app_id, profile in values.get("overrides", {}).items():
                    lines.append(f"[apps.overrides.{_toml_key(app_id)}]")
                    for key, value in profile.items():
                        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
                    lines.append("")
        fd, name = tempfile.mkstemp(prefix=".config-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write("\n".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def update_ai(self, values):
        candidate = copy.deepcopy(self.data)
        candidate["ai"].update(values)
        errors = validate(candidate)
        if errors:
            raise ValueError(errors[0].split(": ", 1)[-1])
        previous = self.data
        self.data = candidate
        try:
            self.save()
        except OSError:
            self.data = previous
            raise

    def hide(self, app_id):
        if not desktop_id(app_id):
            raise ValueError("Нужен desktop ID приложения")
        if app_id not in self.data["apps"]["hidden"]:
            self.data["apps"]["hidden"].append(app_id)
        if app_id in self.data["apps"]["overrides"]:
            self.data["apps"]["overrides"][app_id]["hidden"] = True
        self.save()

    def unhide(self, app_id):
        if not desktop_id(app_id):
            raise ValueError("Нужен desktop ID приложения")
        self.data["apps"]["hidden"] = [x for x in self.data["apps"]["hidden"] if x != app_id]
        if app_id in self.data["apps"]["overrides"]:
            self.data["apps"]["overrides"][app_id]["hidden"] = False
        self.save()

    def app_settings(self, app_id):
        """Return effective settings; missing values inherit the desktop entry."""
        settings = copy.deepcopy(APP_DEFAULTS)
        settings["hidden"] = app_id in self.data["apps"]["hidden"]
        settings.update(copy.deepcopy(self.data["apps"]["overrides"].get(app_id, {})))
        return settings

    def update_app(self, app_id, values):
        if not desktop_id(app_id) or not isinstance(values, dict):
            raise ValueError("Нужен desktop ID и настройки приложения")
        candidate = copy.deepcopy(self.data)
        profile = candidate["apps"]["overrides"].setdefault(app_id, {})
        for key, value in values.items():
            if value is None:
                profile.pop(key, None)
            else:
                profile[key] = copy.deepcopy(value)
        errors = _validate_app_profiles(candidate["apps"])
        if errors:
            raise ValueError("\n".join(errors))
        if "hidden" in values:
            candidate["apps"]["hidden"] = [x for x in candidate["apps"]["hidden"] if x != app_id]
            if profile.get("hidden", False):
                candidate["apps"]["hidden"].append(app_id)
        previous = self.data
        self.data = candidate
        try:
            self.save()
        except BaseException:
            self.data = previous
            raise

    def reset_app(self, app_id):
        if not desktop_id(app_id):
            raise ValueError("Нужен desktop ID приложения")
        previous = self.data
        self.data = copy.deepcopy(previous)
        self.data["apps"]["overrides"].pop(app_id, None)
        self.data["apps"]["hidden"] = [x for x in self.data["apps"]["hidden"] if x != app_id]
        try:
            self.save()
        except BaseException:
            self.data = previous
            raise
