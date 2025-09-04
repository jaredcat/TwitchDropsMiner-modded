from __future__ import annotations

import os
from typing import Any, TypedDict, TYPE_CHECKING

from yarl import URL

from utils import json_load, json_save
from constants import (
    SETTINGS_PATH,
    DEFAULT_LANG,
    PRIORITY_ALGORITHM_LIST,
    PRIORITY_ALGORITHM_BALANCED,
    PRIORITY_ALGORITHM_ADAPTIVE,
    PRIORITY_ALGORITHM_ENDING_SOONEST,
)

if TYPE_CHECKING:
    from main import ParsedArgs


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_theme: bool
    autostart: bool
    exclude: set[str]
    priority: list[str]
    priority_only: bool
    priority_algorithm: str
    unlinked_campaigns: bool
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    window_position: str


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "dark_theme": False,
    "autostart": False,
    "priority_only": True,
    "priority_algorithm": PRIORITY_ALGORITHM_LIST,
    "unlinked_campaigns": False,
    "autostart_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "window_position": "",
}


class Settings:
    # from args
    log: bool
    tray: bool
    no_run_check: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_theme: bool
    autostart: bool
    exclude: set[str]
    priority: list[str]
    priority_only: bool
    priority_algorithm: str
    unlinked_campaigns: bool
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    window_position: str

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self.__get_settings_from_env__()
        self._args: ParsedArgs = args
        self._altered: bool = False

    def __get_settings_from_env__(self):
        if os.environ.get("prioritize_by_ending_soonest") == "1":
            self._settings["priority_algorithm"] = PRIORITY_ALGORITHM_ENDING_SOONEST
        if os.environ.get("UNLINKED_CAMPAIGNS") == "1":
            self._settings["unlinked_campaigns"] = True

        # Migration: Convert old prioritize_by_ending_soonest to new priority_algorithm
        if ("priority_algorithm" not in self._settings):
            if (
                "prioritize_by_ending_soonest" in self._settings
            ):
                if self._settings["prioritize_by_ending_soonest"]:
                    self._settings["priority_algorithm"] = PRIORITY_ALGORITHM_ENDING_SOONEST
                # Remove the old setting
                del self._settings["prioritize_by_ending_soonest"]
                self._altered = True
            else:
                self._settings["priority_algorithm"] = (
                    PRIORITY_ALGORITHM_LIST  # Default to ordered list for existing users
                )

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(SETTINGS_PATH, self._settings, sort=True)
