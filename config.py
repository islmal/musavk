"""Persistent settings stored in config.json next to the application."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "config.json"


def default_download_dir() -> str:
    if sys.platform == "win32":
        # Honour a relocated Downloads folder (registry "Shell Folders" entry).
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
                if value and os.path.isdir(value):
                    return value
        except OSError:
            pass
    return str(Path.home() / "Downloads")


DEFAULTS: dict[str, Any] = {
    "download_dir": "",
    "last_channel": "",
    "last_query": "",
    "sort": 0,
    "quality": 0,          # index into vk_search.QUALITY_OPTIONS
    "window_geometry": "",
}


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()
        if not self.data.get("download_dir"):
            self.data["download_dir"] = default_download_dir()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update(stored)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()
