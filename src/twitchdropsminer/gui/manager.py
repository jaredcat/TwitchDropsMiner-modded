"""
Main GUI manager and theme handling for the Twitch Drops Miner GUI.

This module contains the main GUIManager class that orchestrates all GUI components
and handles theme management.
"""

from __future__ import annotations

import os
import sys
import ctypes
import asyncio
import logging
import tkinter as tk
from pathlib import Path
from collections import abc
from functools import cached_property
from datetime import datetime
from tkinter.font import Font, nametofont
from tkinter import Tk, ttk
from typing import Any, NoReturn, TYPE_CHECKING

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

from ..cache import ImageCache
from ..exceptions import ExitRequest
from ..utils import resource_path, set_root_icon, Game, _T
from ..constants import WINDOW_TITLE, State, OUTPUT_FORMATTER
from ..translate import _
from .components import (
    StatusBar, WebsocketStatus, LoginForm, CampaignProgress,
    ConsoleOutput, ChannelList, TrayIcon, Notebook, _TKOutputHandler
)
from .tabs import InventoryOverview, SettingsPanel, HelpTab

if TYPE_CHECKING:
    from ..twitch import Twitch
    from ..channel import Channel
    from ..inventory import TimedDrop


class GUIManager:
    """Main GUI manager class that orchestrates all GUI components."""

    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._poll_task: asyncio.Task[NoReturn] | None = None
        self._close_requested = asyncio.Event()
        self._root = root = Tk(className=WINDOW_TITLE)
        # withdraw immediately to prevent the window from flashing
        self._root.withdraw()
        # root.resizable(False, True)
        set_root_icon(root, resource_path("pickaxe.ico"))
        root.title(WINDOW_TITLE)  # window title
        root.bind_all("<KeyPress-Escape>", self.unfocus)  # pressing ESC unfocuses selection
        # restore last window position
        if self._twitch.settings.window_position:
            root.geometry(self._twitch.settings.window_position)
        else:
            self._twitch.settings.window_position = self._root.geometry()
        # Image cache for displaying images
        self._cache = ImageCache(self)

        # style adjustments
        self._style = style = ttk.Style(root)
        default_font = nametofont("TkDefaultFont")
        # theme
        theme = ''
        # theme = style.theme_names()[6]
        # style.theme_use(theme)
        # Fix treeview's background color from tags not working (also see '_fixed_map')
        style.map(
            "Treeview",
            foreground=self._fixed_map("foreground"),
            background=self._fixed_map("background"),
        )
        # remove Notebook.focus from the Notebook.Tab layout tree to avoid an ugly dotted line
        # on tab selection. We fold the Notebook.focus children into Notebook.padding children.
        if theme != "classic":
            try:
                original = style.layout("TNotebook.Tab")
                sublayout = original[0][1]["children"][0][1]
                sublayout["children"] = sublayout["children"][0][1]["children"]
                style.layout("TNotebook.Tab", original)
            except (KeyError, IndexError, TypeError):
                # Theme structure is different than expected, skip this customization
                pass
        # add padding to the tab names
        style.configure("TNotebook.Tab", padding=[8, 4])
        # remove Checkbutton.focus dotted line from checkbuttons
        if theme != "classic":
            style.configure("TCheckbutton", padding=0)
            try:
                original = style.layout("TCheckbutton")
                sublayout = original[0][1]["children"]
                sublayout[1] = sublayout[1][1]["children"][0]
                del original[0][1]["children"][1]
                style.layout("TCheckbutton", original)
            except (KeyError, IndexError, TypeError):
                # Theme structure is different than expected, skip this customization
                pass
        # label style - green, yellow and red text
        style.configure("green.TLabel", foreground="green")
        style.configure("yellow.TLabel", foreground="goldenrod")
        style.configure("red.TLabel", foreground="red")
        # label style with a monospace font
        monospaced_font = Font(root, family="Courier New", size=10)
        style.configure("MS.TLabel", font=monospaced_font)
        # button style with a larger font
        large_font = default_font.copy()
        large_font.config(size=12)
        style.configure("Large.TButton", font=large_font)
        # label style that mimics links
        link_font = default_font.copy()
        link_font.config(underline=True)
        style.configure("Link.TLabel", font=link_font, foreground="blue")
        # end of style changes

        root_frame = ttk.Frame(root, padding=8)
        root_frame.grid(column=0, row=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        # Notebook
        self.tabs = Notebook(self, root_frame)
        # Tray icon - place after notebook so it draws on top of the tabs space
        self.tray = TrayIcon(self, root_frame)
        # Main tab
        main_frame = ttk.Frame(root_frame, padding=8)
        self.tabs.add_tab(main_frame, name=_("gui", "tabs", "main"))
        self.status = StatusBar(self, main_frame)
        self.websockets = WebsocketStatus(self, main_frame)
        self.login = LoginForm(self, main_frame)
        self.progress = CampaignProgress(self, main_frame)
        self.output = ConsoleOutput(self, main_frame)
        self.channels = ChannelList(self, main_frame)
        # Inventory tab
        inv_frame = ttk.Frame(root_frame, padding=8)
        self.inv = InventoryOverview(self, inv_frame)
        self.tabs.add_tab(inv_frame, name=_("gui", "tabs", "inventory"))
        # Settings tab
        settings_frame = ttk.Frame(root_frame, padding=8)
        self.settings = SettingsPanel(self, settings_frame, root)
        self.tabs.add_tab(settings_frame, name=_("gui", "tabs", "settings"))
        # Help tab
        help_frame = ttk.Frame(root_frame, padding=8)
        self.help = HelpTab(self, help_frame)
        self.tabs.add_tab(help_frame, name=_("gui", "tabs", "help"))
        # clamp minimum window size (update geometry first)
        root.update_idletasks()
        min_width = root.winfo_reqwidth()
        min_height = root.winfo_reqheight()
        root.minsize(width=min_width, height=min_height)

        # Set dynamic height based on screen size for better Settings tab experience
        screen_height = root.winfo_screenheight()
        # Use 80% of screen height but ensure it's at least the minimum required height
        dynamic_height = max(min_height, int(screen_height * 0.8))

        # Apply dynamic height adjustment - either new window or expand existing window height
        if not self._twitch.settings.window_position:
            # New window - set default size with dynamic height
            root.geometry(f"{min_width}x{dynamic_height}")
            self._twitch.settings.window_position = root.geometry()
        else:
            # Existing window - parse current geometry and apply dynamic height if beneficial
            current_geom = self._twitch.settings.window_position
            if 'x' in current_geom and '+' in current_geom:
                # Parse geometry string like "940x690+3203+345"
                size_part = current_geom.split('+')[0]  # "940x690"
                position_part = current_geom[len(size_part):]  # "+3203+345"
                if 'x' in size_part:
                    current_width, current_height = map(int, size_part.split('x'))
                    # Use dynamic height if it's larger than current height
                    new_height = max(current_height, dynamic_height)
                    new_geometry = f"{current_width}x{new_height}{position_part}"
                    root.geometry(new_geometry)
                    self._twitch.settings.window_position = new_geometry
        # register logging handler
        self._handler = _TKOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger = logging.getLogger("TwitchDrops")
        logger.addHandler(self._handler)
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")
        # gracefully handle Windows shutdown closing the application
        if sys.platform == "win32":
            # NOTE: this root.update() is required for the below to work - don't remove
            root.update()
            self._message_map = {
                # window close request
                win32con.WM_CLOSE: self.close,
                # shutdown request
                win32con.WM_QUERYENDSESSION: self.close,
            }
            # This hooks up the wnd_proc function as the message processor for the root window.
            self.old_wnd_proc = win32gui.SetWindowLong(
                self._handle, win32con.GWL_WNDPROC, self.wnd_proc
            )
            # This ensures all of this works when the application is withdrawn or iconified
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self._handle, ctypes.c_wchar_p(_("gui", "status", "exiting"))
            )
            # DEV NOTE: use this to remove the reason in the future
            # ctypes.windll.user32.ShutdownBlockReasonDestroy(self._handle)
        else:
            # use old-style window closing protocol for non-windows platforms
            root.protocol("WM_DELETE_WINDOW", self.close)
            root.protocol("WM_DESTROY_WINDOW", self.close)
        # stay hidden in tray if needed, otherwise show the window when everything's ready
        if self._twitch.settings.tray:
            # NOTE: this starts the tray icon thread
            self._root.after_idle(self.tray.minimize)
        else:
            self._root.after_idle(self._root.deiconify)

    # https://stackoverflow.com/questions/56329342/tkinter-treeview-background-tag-not-working
    def _fixed_map(self, option):
        # Fix for setting text colour for Tkinter 8.6.9
        # From: https://core.tcl.tk/tk/info/509cafafae
        #
        # Returns the style map for 'option' with any styles starting with
        # ('!disabled', '!selected', ...) filtered out.

        # style.map() returns an empty list for missing options, so this
        # should be future-safe.
        return [
            elm for elm in self._style.map("Treeview", query_opt=option)
            if elm[:2] != ("!disabled", "!selected")
        ]

    def wnd_proc(self, hwnd, msg, w_param, l_param):
        """
        This function serves as a message processor for all messages sent
        to the application by Windows.
        """
        if msg == win32con.WM_DESTROY:
            win32api.SetWindowLong(self._handle, win32con.GWL_WNDPROC, self.old_wnd_proc)
        if msg in self._message_map:
            return self._message_map[msg](w_param, l_param)
        return win32gui.CallWindowProc(self.old_wnd_proc, hwnd, msg, w_param, l_param)

    @cached_property
    def _handle(self) -> int:
        return int(self._root.wm_frame(), 16)

    @property
    def running(self) -> bool:
        return self._poll_task is not None

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self):
        # wait until the user closes the window
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        # In Python 3.11, we need to explicitly wrap awaitables
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done: set[asyncio.Task[Any]]
        pending: set[asyncio.Task[Any]]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self):
        self._close_requested.clear()

    def start(self):
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll())
        # Start the tray icon now that we have an event loop
        self.tray.start()
        # Set theme after GUI is fully initialized
        if self._twitch.settings.dark_theme:
            set_theme(self._root, self, "dark")
        else:
            set_theme(self._root, self, "default")
        # self.progress.start_timer()

    def stop(self):
        self.progress.stop_timer()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll(self):
        """
        This runs the Tkinter event loop via asyncio instead of calling mainloop.
        0.05s gives similar performance and CPU usage.
        Not ideal, but the simplest way to avoid threads, thread safety,
        loop.call_soon_threadsafe, futures and all of that.
        """
        update = self._root.update
        while True:
            try:
                update()
            except tk.TclError:
                # root has been destroyed
                break
            await asyncio.sleep(0.05)
        self._poll_task = None

    def close(self, *args) -> int:
        """
        Requests the GUI application to close.
        The window itself will be closed in the closing sequence later.
        """
        self._close_requested.set()
        # notify client we're supposed to close
        self._twitch.close()
        return 0

    def close_window(self):
        """
        Closes the window. Invalidates the logger.
        """
        self.tray.stop()
        logging.getLogger("TwitchDrops").removeHandler(self._handler)
        self._root.destroy()

    def unfocus(self, event):
        # support pressing ESC to unfocus
        self._root.focus_set()
        self.channels.clear_selection()
        self.settings.clear_selection()

    # these are here to interface with underlaying GUI components
    def save(self, *, force: bool = False) -> None:
        self._twitch.settings.window_position = self._root.geometry()
        self._cache.save(force=force)

    def grab_attention(self, *, sound: bool = True):
        self.tray.restore()
        self._root.focus_force()
        if sound:
            self._root.bell()

    def set_games(self, games: abc.Iterable[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)  # main tab
        # inventory overview is updated from within drops themselves via change events
        self.tray.update_title(drop)  # tray

    def clear_drop(self):
        self.progress.display(None)
        self.tray.update_title(None)

    def print(self, message: str):
        print(f"{datetime.now().strftime('%Y-%m-%d %X')}: {message}")
        # print to our custom output
        self.output.print(message)


def set_theme(root, manager, name):
    """Set the application theme (dark or light)."""
    style = ttk.Style(root)
    if not hasattr(set_theme, "default_style"):
        set_theme.default_style = style.theme_use()         # "Themes" is more fitting for the recolour and "Style" for the button style.

    default_font = nametofont("TkDefaultFont")
    large_font = default_font.copy()
    large_font.config(size=12)
    link_font = default_font.copy()
    link_font.config(underline=True)

    def configure_combobox_list(combobox, flag, value):
        try:
            combobox.update_idletasks()
            popdown_window = combobox.tk.call("ttk::combobox::PopdownWindow", combobox)
            listbox = f"{popdown_window}.f.l"
            # Check if the listbox exists before trying to configure it
            if combobox.tk.call("winfo", "exists", listbox):
                combobox.tk.call(listbox, "configure", flag, value)
        except tk.TclError:
            # Combobox popdown doesn't exist yet, skip
            pass

    # Style options, !!!"background" and "bg" is not interchangable for some reason!!!
    if name == "dark":
        bg_grey = "#181818"
        active_grey = "#2b2b2b"
        # General
        style.theme_use('alt')      # We have to switch the theme, because OS-defaults ("vista") don't support certain customisations, like Treeview-fieldbackground etc.
        style.configure('.', background=bg_grey, foreground="white")
        style.configure("Link.TLabel", font=link_font, foreground="#00aaff")
        # Buttons
        style.map("TButton",
                  background=[("active", active_grey)])
        # Tabs
        style.configure("TNotebook.Tab", background=bg_grey)
        style.map("TNotebook.Tab",
                  background=[("selected", active_grey)])
        # Checkboxes
        style.configure("TCheckbutton", foreground="black") # The checkbox has to be white since it's an image, so the tick has to be black
        style.map("TCheckbutton",
                  background=[('active', active_grey)])
        # Output field
        manager.output._text.configure(bg=bg_grey, fg="white", selectbackground=active_grey)
        # Include/Exclude lists
        manager.settings._exclude_list.configure(bg=bg_grey, fg="white")
        manager.settings._priority_list.configure(bg=bg_grey, fg="white")
        # Channel list
        style.configure('Treeview', background=bg_grey, fieldbackground=bg_grey)
        manager.channels._table
        # Inventory
        manager.inv._canvas.configure(bg=bg_grey)
        # Scroll bars
        style.configure("TScrollbar", foreground="white", troughcolor=bg_grey, bordercolor=bg_grey,  arrowcolor="white")
        style.map("TScrollbar",
                  background=[("active", bg_grey), ("!active", bg_grey)])
        # Language selection box _select_menu
        manager.settings._select_menu.configure(bg=bg_grey, fg="white", activebackground=active_grey, activeforeground="white") # Couldn't figure out how to change the border, so it stays black
        for index in range(manager.settings._select_menu.menu.index("end")+1):
             manager.settings._select_menu.menu.entryconfig(index, background=bg_grey, activebackground=active_grey, foreground="white")
        # Proxy field
        style.configure("TEntry", foreground="white", selectbackground=active_grey, fieldbackground=bg_grey)
        # Include/Exclude box
        style.configure("TCombobox", foreground="white", selectbackground=active_grey, fieldbackground=bg_grey, arrowcolor="white")
        style.map("TCombobox", background=[("active", active_grey), ("disabled", bg_grey)])
        # Include list
        configure_combobox_list(manager.settings._priority_entry, "-background", bg_grey)
        configure_combobox_list(manager.settings._priority_entry, "-foreground", "white")
        configure_combobox_list(manager.settings._priority_entry, "-selectbackground", active_grey)
        # Exclude list
        configure_combobox_list(manager.settings._exclude_entry, "-background", bg_grey)
        configure_combobox_list(manager.settings._exclude_entry, "-foreground", "white")
        configure_combobox_list(manager.settings._exclude_entry, "-selectbackground", active_grey)

    else: # When creating a new theme, additional values might need to be set, so the default theme remains consistent
        # General
        style.theme_use(set_theme.default_style)
        style.configure('.', background="#f0f0f0", foreground="#000000")
        # Buttons
        style.map("TButton",
                  background=[("active", "#ffffff")])
        # Tabs
        style.configure("TNotebook.Tab", background="#f0f0f0")
        style.map("TNotebook.Tab",
                  background=[("selected", "#ffffff")])
        # Checkboxes don't need to be reverted
        # Output field
        manager.output._text.configure(bg="#ffffff", fg="#000000")
        # Include/Exclude lists
        manager.settings._exclude_list.configure(bg="#ffffff", fg="#000000")
        manager.settings._priority_list.configure(bg="#ffffff", fg="#000000")
        # Channel list doesn't need to be reverted
        # Inventory
        manager.inv._canvas.configure(bg="#f0f0f0")
        # Scroll bars don't need to be reverted
        # Language selection box _select_menu
        manager.settings._select_menu.configure(bg="#ffffff", fg="black", activebackground="#f0f0f0", activeforeground="black") # Couldn't figure out how to change the border, so it stays black
        for index in range(manager.settings._select_menu.menu.index("end")+1):
             manager.settings._select_menu.menu.entryconfig(index, background="#f0f0f0", activebackground="#0078d7", foreground="black")
        # Proxy field doesn't need to be reverted
        # Include/Exclude dropdown - Only the lists have to be reverted
        # Include list
        configure_combobox_list(manager.settings._priority_entry, "-background", "white")
        configure_combobox_list(manager.settings._priority_entry, "-foreground", "black")
        configure_combobox_list(manager.settings._priority_entry, "-selectbackground", "#0078d7")
        # Exclude list
        configure_combobox_list(manager.settings._exclude_entry, "-background", "white")
        configure_combobox_list(manager.settings._exclude_entry, "-foreground", "black")
        configure_combobox_list(manager.settings._exclude_entry, "-selectbackground", "#0078d7")
