"""
Main application logic for Twitch Drops Miner.

This module contains the main application execution logic,
separated from argument parsing for better modularity.
"""

from __future__ import annotations

import sys
import signal
import asyncio
import logging
import warnings
import traceback
from typing import NoReturn

from .translate import _
from .twitch import Twitch
from .settings import Settings
from .exceptions import CaptchaRequired
from .utils import lock_file
from .constants import CALL, FILE_FORMATTER, LOG_PATH, LOCK_PATH
from .cli import parse_arguments


warnings.simplefilter("default", ResourceWarning)


async def main():
    """Main application entry point."""
    # Parse command line arguments
    args = parse_arguments()

    # Load settings
    try:
        settings = Settings(args)
    except Exception:
        import tkinter as tk
        from tkinter import messagebox
        messagebox.showerror(
            "Settings error",
            f"There was an error while loading the settings file:\n\n{traceback.format_exc()}"
        )
        sys.exit(4)

    # Set language
    try:
        _.set_language(settings.language)
    except ValueError:
        # this language doesn't exist - stick to English
        pass

    # Handle logging setup
    if settings.logging_level > logging.DEBUG:
        # redirect the root logger into a NullHandler, effectively ignoring all logging calls
        # that aren't ours. This always runs, unless the main logging level is DEBUG or lower.
        logging.getLogger().addHandler(logging.NullHandler())
    logger = logging.getLogger("TwitchDrops")
    logger.setLevel(settings.logging_level)
    if settings.log:
        handler = logging.FileHandler(LOG_PATH)
        handler.setFormatter(FILE_FORMATTER)
        logger.addHandler(handler)
    logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
    logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

    exit_status = 0
    client = Twitch(settings)
    loop = asyncio.get_running_loop()
    if sys.platform == "linux":
        loop.add_signal_handler(signal.SIGINT, lambda *_: client.gui.close())
        loop.add_signal_handler(signal.SIGTERM, lambda *_: client.gui.close())
    try:
        await client.run()
    except CaptchaRequired:
        exit_status = 1
        client.prevent_close()
        client.print(_("error", "captcha"))
    except Exception:
        exit_status = 1
        client.prevent_close()
        client.print("Fatal error encountered:\n")
        client.print(traceback.format_exc())
    finally:
        if sys.platform == "linux":
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
        client.print(_("gui", "status", "exiting"))
        await client.shutdown()
    if not client.gui.close_requested:
        # user didn't request the closure
        client.print(_("status", "terminated"))
        client.gui.status.update(_("gui", "status", "terminated"))
        # notify the user about the closure
        client.gui.grab_attention(sound=True)
    await client.gui.wait_until_closed()
    # save the application state
    # NOTE: we have to do it after wait_until_closed,
    # because the user can alter some settings between app termination and closing the window
    client.save(force=True)
    client.gui.stop()
    client.gui.close_window()
    sys.exit(exit_status)


def run_app() -> NoReturn:
    """Run the application with proper setup and cleanup."""
    print(f"{__import__('datetime').datetime.now().strftime('%Y-%m-%d %X')}: Starting: Twitch Drops Miner")

    # PyInstaller freeze support
    from multiprocessing import freeze_support
    freeze_support()

    # SSL trust store injection for Python 3.10+
    if sys.version_info >= (3, 10):
        import truststore
        truststore.inject_into_ssl()

    try:
        # Use lock_file to check if we're not already running
        success, file = lock_file(LOCK_PATH)
        if not success:
            # already running - exit
            sys.exit(3)

        asyncio.run(main())
    finally:
        file.close()


if __name__ == "__main__":
    run_app()
