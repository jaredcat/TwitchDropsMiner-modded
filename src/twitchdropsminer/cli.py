"""
Command line interface for Twitch Drops Miner.

This module handles argument parsing and provides a clean interface
for command line options.
"""

from __future__ import annotations

import io
import sys
import argparse
import tkinter as tk
from tkinter import messagebox
from typing import IO, NoReturn

from .version import __version__
from .constants import SELF_PATH


class Parser(argparse.ArgumentParser):
    """Custom argument parser that shows errors in message boxes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._message: io.StringIO = io.StringIO()

    def _print_message(self, message: str, file: IO[str] | None = None) -> None:
        self._message.write(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        try:
            super().exit(status, message)  # sys.exit(2)
        finally:
            messagebox.showerror("Argument Parser Error", self._message.getvalue())


class ParsedArgs(argparse.Namespace):
    """Parsed command line arguments with computed properties."""

    _verbose: int
    _debug_ws: bool
    _debug_gql: bool
    log: bool
    tray: bool
    no_run_check: bool

    # TODO: replace int with union of literal values once typeshed updates
    @property
    def logging_level(self) -> int:
        import logging
        from .constants import CALL
        return {
            0: logging.ERROR,
            1: logging.WARNING,
            2: logging.INFO,
            3: CALL,
            4: logging.DEBUG,
        }[min(self._verbose, 4)]

    @property
    def debug_ws(self) -> int:
        """
        If the debug flag is True, return DEBUG.
        If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
        Otherwise, return NOTSET to inherit the global logging level.
        """
        import logging
        if self._debug_ws:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET

    @property
    def debug_gql(self) -> int:
        import logging
        if self._debug_gql:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET


def parse_arguments() -> ParsedArgs:
    """Parse command line arguments and return parsed namespace."""
    # Create a dummy invisible window for the parser
    root = tk.Tk()
    root.overrideredirect(True)
    root.withdraw()
    from .utils import resource_path, set_root_icon
    set_root_icon(root, resource_path("pickaxe.ico"))
    root.update()

    parser = Parser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    # debug options
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true"
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true"
    )

    args = parser.parse_args(namespace=ParsedArgs())

    # Clean up dummy window
    root.destroy()
    del root, parser

    return args
