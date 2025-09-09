"""
Complex UI components for the Twitch Drops Miner GUI.

This module contains complex UI components like forms, progress displays,
channel lists, and other major interface elements.
"""

from __future__ import annotations

import os
import re
import sys
import asyncio
import logging
import tkinter as tk
from pathlib import Path
from collections import abc
from dataclasses import dataclass
from math import log10, ceil
from time import time
from tkinter.font import Font, nametofont
from functools import partial, cached_property
from datetime import datetime, timedelta, timezone
from tkinter import ttk, StringVar, DoubleVar, IntVar
from typing import Any, Union, Tuple, TypedDict, NoReturn, TYPE_CHECKING

import pystray
from yarl import URL
from PIL.ImageTk import PhotoImage
from PIL import Image as Image_module

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

from ..cache import CurrentSeconds
from ..translate import _
from ..cache import ImageCache
from ..exceptions import ExitRequest
from ..utils import resource_path, set_root_icon, webopen, Game, _T
from ..constants import (
    SELF_PATH, OUTPUT_FORMATTER, WS_TOPICS_LIMIT, MAX_WEBSOCKETS, WINDOW_TITLE, State,
    PRIORITY_ALGORITHM_LIST, PRIORITY_ALGORITHM_ADAPTIVE, PRIORITY_ALGORITHM_BALANCED, PRIORITY_ALGORITHM_ENDING_SOONEST
)

if TYPE_CHECKING:
    from ..twitch import Twitch
    from ..channel import Channel
    from ..settings import Settings
    from ..inventory import DropsCampaign, TimedDrop
    from .manager import GUIManager


DIGITS = ceil(log10(WS_TOPICS_LIMIT))


class _TKOutputHandler(logging.Handler):
    """Custom logging handler that outputs to the GUI."""

    def __init__(self, output: 'GUIManager'):
        super().__init__()
        self._output = output

    def emit(self, record):
        self._output.print(self.format(record))


class StatusBar:
    """Status bar component for displaying current status."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "status", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=0, columnspan=3, sticky="nsew", padx=2)
        self._label = ttk.Label(frame)
        self._label.grid(column=0, row=0, sticky="nsew")

    def update(self, text: str):
        self._label.config(text=text)

    def clear(self):
        self._label.config(text='')


class _WSEntry(TypedDict):
    """Websocket entry data structure."""
    status: str
    topics: int


class WebsocketStatus:
    """Websocket status display component."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "websocket", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=1, sticky="nsew", padx=2)
        self._status_var = StringVar(frame)
        self._topics_var = StringVar(frame)
        ttk.Label(
            frame,
            text='\n'.join(
                _("gui", "websocket", "websocket").format(id=i)
                for i in range(1, MAX_WEBSOCKETS + 1)
            ),
            style="MS.TLabel",
        ).grid(column=0, row=0)
        ttk.Label(
            frame,
            textvariable=self._status_var,
            width=16,
            justify="left",
            style="MS.TLabel",
        ).grid(column=1, row=0)
        ttk.Label(
            frame,
            textvariable=self._topics_var,
            width=(DIGITS * 2 + 1),
            justify="right",
            style="MS.TLabel",
        ).grid(column=2, row=0)
        self._items: dict[int, _WSEntry | None] = {i: None for i in range(MAX_WEBSOCKETS)}
        self._update()

    def update(self, idx: int, status: str | None = None, topics: int | None = None):
        if status is None and topics is None:
            raise TypeError("You need to provide at least one of: status, topics")
        entry = self._items.get(idx)
        if entry is None:
            entry = self._items[idx] = _WSEntry(
                status=_("gui", "websocket", "disconnected"), topics=0
            )
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        self._update()

    def remove(self, idx: int):
        if idx in self._items:
            del self._items[idx]
            self._update()

    def _update(self):
        status_lines: list[str] = []
        topic_lines: list[str] = []
        for idx in range(MAX_WEBSOCKETS):
            if (item := self._items.get(idx)) is None:
                status_lines.append('')
                topic_lines.append('')
            else:
                status_lines.append(item["status"])
                topic_lines.append(f"{item['topics']:>{DIGITS}}/{WS_TOPICS_LIMIT}")
        self._status_var.set('\n'.join(status_lines))
        self._topics_var.set('\n'.join(topic_lines))


@dataclass
class LoginData:
    """Data structure for login information."""
    username: str
    password: str
    token: str


class LoginForm:
    """Login form component."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        from .widgets import PlaceholderEntry

        self._manager = manager
        self._var = StringVar(master)
        frame = ttk.LabelFrame(master, text=_("gui", "login", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=1, row=1, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text=_("gui", "login", "labels")).grid(column=0, row=0)
        ttk.Label(frame, textvariable=self._var, justify="center").grid(column=1, row=0)
        self._login_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "username"))
        # self._login_entry.grid(column=0, row=1, columnspan=2)
        self._pass_entry = PlaceholderEntry(
            frame, placeholder=_("gui", "login", "password"), show='•'
        )
        # self._pass_entry.grid(column=0, row=2, columnspan=2)
        self._token_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "twofa_code"))
        # self._token_entry.grid(column=0, row=3, columnspan=2)

        self._confirm = asyncio.Event()
        self._button = ttk.Button(
            frame, text=_("gui", "login", "button"), command=self._confirm.set, state="disabled"
        )
        self._button.grid(column=0, row=4, columnspan=2)
        self.update(_("gui", "login", "logged_out"), None)

    def clear(self, login: bool = False, password: bool = False, token: bool = False):
        clear_all = not login and not password and not token
        if login or clear_all:
            self._login_entry.clear()
        if password or clear_all:
            self._pass_entry.clear()
        if token or clear_all:
            self._token_entry.clear()

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        try:
            self._button.config(state="normal")
            await self._manager.coro_unless_closed(self._confirm.wait())
        finally:
            self._button.config(state="disabled")

    async def ask_login(self) -> LoginData:
        self.update(_("gui", "login", "required"), None)
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        while True:
            self._manager.print(_("gui", "login", "request"))
            await self.wait_for_login_press()
            login_data = LoginData(
                self._login_entry.get().strip(),
                self._pass_entry.get(),
                self._token_entry.get().strip(),
            )
            # basic input data validation: 3-25 characters in length, only ascii and underscores
            if (
                not 3 <= len(login_data.username) <= 25
                and re.match(r'^[a-zA-Z0-9_]+$', login_data.username)
            ):
                self.clear(login=True)
                continue
            if len(login_data.password) < 8:
                self.clear(password=True)
                continue
            if login_data.token and len(login_data.token) < 6:
                self.clear(token=True)
                continue
            return login_data

    async def ask_enter_code(self, user_code: str) -> None:
        self.update(_("gui", "login", "required"), None)
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        self._manager.print(_("gui", "login", "request"))
        await self.wait_for_login_press()
        self._manager.print(_("gui", "login", "enter_code").format(user_code=user_code))
        webopen("https://www.twitch.tv/activate")

    def update(self, status: str, user_id: int | None):
        if user_id is not None:
            user_str = str(user_id)
        else:
            user_str = "-"
        self._var.set(f"{status}\n{user_str}")


class _BaseVars(TypedDict):
    """Base variables for progress displays."""
    progress: DoubleVar
    percentage: StringVar
    remaining: StringVar


class _CampaignVars(_BaseVars):
    """Campaign-specific progress variables."""
    name: StringVar
    game: StringVar


class _DropVars(_BaseVars):
    """Drop-specific progress variables."""
    rewards: StringVar


class _ProgressVars(TypedDict):
    """All progress variables."""
    campaign: _CampaignVars
    drop: _DropVars


class CampaignProgress:
    """Campaign progress display component."""

    BAR_LENGTH = 420

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._manager = manager
        self._vars: _ProgressVars = {
            "campaign": {
                "name": StringVar(master),  # campaign name
                "game": StringVar(master),  # game name
                "progress": DoubleVar(master),  # controls the progress bar
                "percentage": StringVar(master),  # percentage display string
                "remaining": StringVar(master),  # time remaining string, filled via _update_time
            },
            "drop": {
                "rewards": StringVar(master),  # drop rewards
                "progress": DoubleVar(master),  # as above
                "percentage": StringVar(master),  # as above
                "remaining": StringVar(master),  # as above
            },
        }
        self._frame = frame = ttk.LabelFrame(
            master, text=_("gui", "progress", "name"), padding=(4, 0, 4, 4)
        )
        frame.grid(column=0, row=2, columnspan=2, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        game_campaign = ttk.Frame(frame)
        game_campaign.grid(column=0, row=0, columnspan=2, sticky="nsew")
        game_campaign.columnconfigure(0, weight=1)
        game_campaign.columnconfigure(1, weight=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "game")).grid(column=0, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["game"]).grid(column=0, row=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "campaign")).grid(column=1, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["name"]).grid(column=1, row=1)
        ttk.Label(
            frame, text=_("gui", "progress", "campaign_progress")
        ).grid(column=0, row=2, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["percentage"]).grid(column=1, row=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["remaining"]).grid(column=1, row=3)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["campaign"]["progress"],
        ).grid(column=0, row=4, columnspan=2)
        ttk.Separator(
            frame, orient="horizontal"
        ).grid(row=5, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(frame, text=_("gui", "progress", "drop")).grid(column=0, row=6, columnspan=2)
        ttk.Label(
            frame, textvariable=self._vars["drop"]["rewards"]
        ).grid(column=0, row=7, columnspan=2)
        ttk.Label(
            frame, text=_("gui", "progress", "drop_progress")
        ).grid(column=0, row=8, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["drop"]["percentage"]).grid(column=1, row=8)
        ttk.Label(frame, textvariable=self._vars["drop"]["remaining"]).grid(column=1, row=9)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["drop"]["progress"],
        ).grid(column=0, row=10, columnspan=2)
        self._drop: TimedDrop | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self.display(None)

    @staticmethod
    def _divmod(minutes: int, seconds: int) -> tuple[int, int]:
        if seconds < 60 and minutes > 0:
            minutes -= 1
        hours, minutes = divmod(minutes, 60)
        return (hours, minutes)

    def _update_time(self, seconds: int):
        drop = self._drop
        if drop is not None:
            drop_minutes = drop.remaining_minutes
            campaign_minutes = drop.campaign.remaining_minutes
        else:
            drop_minutes = 0
            campaign_minutes = 0
        drop_vars: _DropVars = self._vars["drop"]
        campaign_vars: _CampaignVars = self._vars["campaign"]
        dseconds = seconds % 60
        CurrentSeconds.set_current_seconds(dseconds)
        hours, minutes = self._divmod(drop_minutes, seconds)
        drop_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )
        hours, minutes = self._divmod(campaign_minutes, seconds)
        campaign_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )

    async def _timer_loop(self):
        seconds = 60
        self._update_time(seconds)
        while seconds > 0:
            await asyncio.sleep(1)
            seconds -= 1
            self._update_time(seconds)
        self._timer_task = None

    def start_timer(self):
        self._manager.print(_("gui", "progress", "progress_update").format(
            current_minutes=self._drop.current_minutes,
            required_minutes=self._drop.required_minutes,
            campaign=self._drop.campaign
        ))
        with open('healthcheck.timestamp', 'w') as f:
            f.write(str(int(time())))
        if self._timer_task is None:
            if self._drop is None or self._drop.remaining_minutes <= 0:
                # if we're starting the timer at 0 drop minutes,
                # all we need is a single instant time update setting seconds to 60,
                # to avoid substracting a minute from campaign minutes
                self._update_time(60)
            else:
                self._timer_task = asyncio.create_task(self._timer_loop())

    def stop_timer(self):
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False):
        self._drop = drop
        vars_drop = self._vars["drop"]
        vars_campaign = self._vars["campaign"]
        self.stop_timer()
        if drop is None:
            # clear the drop display
            vars_drop["rewards"].set("...")
            vars_drop["progress"].set(0.0)
            vars_drop["percentage"].set("-%")
            vars_campaign["name"].set("...")
            vars_campaign["game"].set("...")
            vars_campaign["progress"].set(0.0)
            vars_campaign["percentage"].set("-%")
            self._update_time(0)
            return
        vars_drop["rewards"].set(drop.rewards_text())
        vars_drop["progress"].set(drop.progress)
        vars_drop["percentage"].set(f"{drop.progress:6.1%}")
        campaign = drop.campaign
        vars_campaign["name"].set(campaign.name)
        vars_campaign["game"].set(campaign.game.name)
        vars_campaign["progress"].set(campaign.progress)
        vars_campaign["percentage"].set(
            f"{campaign.progress:6.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        if countdown:
            # restart our seconds update timer
            self.start_timer()
        elif subone:
            # display the current remaining time at 0 seconds (after substracting the minute)
            # this is because the watch loop will substract this minute
            # right after the first watch payload returns with a time update
            self._update_time(0)
        else:
            # display full time with no substracting
            self._update_time(60)


class ConsoleOutput:
    """Console output display component."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "output"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=3, columnspan=3, sticky="nsew", padx=2)
        # tell master frame that the containing row can expand
        master.rowconfigure(3, weight=1)
        frame.rowconfigure(0, weight=1)  # let the frame expand
        frame.columnconfigure(0, weight=1)
        xscroll = ttk.Scrollbar(frame, orient="horizontal")
        yscroll = ttk.Scrollbar(frame, orient="vertical")
        self._text = tk.Text(
            frame,
            width=52,
            height=10,
            wrap="none",
            state="disabled",
            exportselection=False,
            xscrollcommand=xscroll.set,
            yscrollcommand=yscroll.set,
        )
        xscroll.config(command=self._text.xview)
        yscroll.config(command=self._text.yview)
        self._text.grid(column=0, row=0, sticky="nsew")
        xscroll.grid(column=0, row=1, sticky="ew")
        yscroll.grid(column=1, row=0, sticky="ns")

    def print(self, message: str):
        stamp = datetime.now().strftime("%X")
        if '\n' in message:
            message = message.replace('\n', f"\n{stamp}: ")
        self._text.config(state="normal")
        self._text.insert("end", f"{stamp}: {message}\n")
        self._text.see("end")  # scroll to the newly added line
        self._text.config(state="disabled")


class _Buttons(TypedDict):
    """Button components for channel list."""
    frame: ttk.Frame
    switch: ttk.Button
    load_points: ttk.Button


class ChannelList:
    """Channel list display component."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._manager = manager
        frame = ttk.LabelFrame(master, text=_("gui", "channels", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=2, row=1, rowspan=2, sticky="nsew", padx=2)
        # tell master frame that the containing column can expand
        master.columnconfigure(2, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        buttons_frame = ttk.Frame(frame)
        self._buttons: _Buttons = {
            "frame": buttons_frame,
            "switch": ttk.Button(
                buttons_frame,
                text=_("gui", "channels", "switch"),
                state="disabled",
                command=manager._twitch.state_change(State.CHANNEL_SWITCH),
            ),
            "load_points": ttk.Button(
                buttons_frame, text=_("gui", "channels", "load_points"), command=self._load_points
            ),
        }
        buttons_frame.grid(column=0, row=0, columnspan=2)
        self._buttons["switch"].grid(column=0, row=0)
        self._buttons["load_points"].grid(column=1, row=0)
        scroll = ttk.Scrollbar(frame, orient="vertical")
        self._table = table = ttk.Treeview(
            frame,
            # columns definition is updated by _add_column
            yscrollcommand=scroll.set,
        )
        scroll.config(command=table.yview)
        table.grid(column=0, row=1, sticky="nsew")
        scroll.grid(column=1, row=1, sticky="ns")
        self._font = Font(frame, manager._style.lookup("Treeview", "font"))
        self._const_width: set[str] = set()
        table.tag_configure("watching", background="gray70")
        table.bind("<Button-1>", self._disable_column_resize)
        table.bind("<<TreeviewSelect>>", self._selected)
        self._add_column("#0", '', width=0)
        self._add_column(
            "channel", _("gui", "channels", "headings", "channel"), width=100, anchor='w'
        )
        self._add_column(
            "status",
            _("gui", "channels", "headings", "status"),
            width_template=[
                _("gui", "channels", "online"),
                _("gui", "channels", "pending"),
                _("gui", "channels", "offline"),
            ],
        )
        self._add_column("game", _("gui", "channels", "headings", "game"), width=50)
        self._add_column("drops", "🎁", width_template="✔")
        self._add_column(
            "viewers", _("gui", "channels", "headings", "viewers"), width_template="1234567"
        )
        self._add_column(
            "points", _("gui", "channels", "headings", "points"), width_template="1234567"
        )
        self._add_column("acl_base", "📋", width_template="✔")
        self._channel_map: dict[str, Channel] = {}

    def _add_column(
        self,
        cid: str,
        name: str,
        *,
        anchor: tk._Anchor = "center",
        width: int | None = None,
        width_template: str | list[str] | None = None,
    ):
        table = self._table
        # NOTE: we don't do this for the icon column
        if cid != "#0":
            # we need to save the column settings and headings before modifying the columns...
            columns: tuple[str, ...] = table.cget("columns") or ()
            column_settings: dict[str, tuple[str, tk._Anchor, int, int]] = {}
            for s_cid in columns:
                s_column = table.column(s_cid)
                assert s_column is not None
                s_heading = table.heading(s_cid)
                assert s_heading is not None
                column_settings[s_cid] = (
                    s_heading["text"], s_heading["anchor"], s_column["width"], s_column["minwidth"]
                )
            # ..., then add the column
            table.config(columns=columns + (cid,))
            # ..., and then restore column settings and headings afterwards
            for s_cid, (s_name, s_anchor, s_width, s_minwidth) in column_settings.items():
                table.heading(s_cid, text=s_name, anchor=s_anchor)
                table.column(s_cid, minwidth=s_minwidth, width=s_width, stretch=False)
        # set heading and column settings for the new column
        if width_template is not None:
            if isinstance(width_template, str):
                width = self._measure(width_template)
            else:
                width = max((self._measure(template) for template in width_template), default=20)
            self._const_width.add(cid)
        assert width is not None
        table.heading(cid, text=name, anchor=anchor)
        table.column(cid, minwidth=width, width=width, stretch=False)

    def _disable_column_resize(self, event):
        if self._table.identify_region(event.x, event.y) == "separator":
            return "break"

    def _selected(self, event):
        selection = self._table.selection()
        if selection:
            self._buttons["switch"].config(state="normal")
        else:
            self._buttons["switch"].config(state="disabled")

    def _load_points(self):
        # disable the button afterwards
        self._buttons["load_points"].config(state="disabled")
        asyncio.gather(*(ch.claim_bonus() for ch in self._manager._twitch.channels.values()))

    def _measure(self, text: str) -> int:
        # we need this because columns have 9-10 pixels of padding that cuts text off
        return self._font.measure(text) + 10

    def _redraw(self):
        # this forces a redraw that recalculates widget width
        self._table.event_generate("<<ThemeChanged>>")

    def _adjust_width(self, column: str, value: str):
        # causes the column to expand if the value's width is greater than the current width
        if column in self._const_width:
            return
        value_width = self._measure(value)
        curr_width = self._table.column(column, "width")
        if value_width > curr_width:
            self._table.column(column, width=value_width)
            self._redraw()

    def shrink(self):
        # causes the columns to shrink back after long values have been removed from it
        columns = self._table.cget("columns")
        iids = self._table.get_children()
        for column in columns:
            if column in self._const_width:
                continue
            if iids:
                # table has at least one item
                width = max(self._measure(self._table.set(i, column)) for i in iids)
                self._table.column(column, width=width)
            else:
                # no items - use minwidth
                minwidth = self._table.column(column, "minwidth")
                self._table.column(column, width=minwidth)
        self._redraw()

    def _set(self, iid: str, column: str, value: str):
        self._table.set(iid, column, value)
        self._adjust_width(column, value)

    def _insert(self, iid: str, values: dict[str, str]):
        to_insert: list[str] = []
        for cid in self._table.cget("columns"):
            value = values[cid]
            to_insert.append(value)
            self._adjust_width(cid, value)
        self._table.insert(parent='', index="end", iid=iid, values=to_insert)

    def clear_watching(self):
        for iid in self._table.tag_has("watching"):
            self._table.item(iid, tags='')

    def set_watching(self, channel: Channel):
        self.clear_watching()
        iid = channel.iid
        self._table.item(iid, tags="watching")
        self._table.see(iid)

    def get_selection(self) -> Channel | None:
        if not self._channel_map:
            return None
        selection = self._table.selection()
        if not selection:
            return None
        return self._channel_map[selection[0]]

    def clear_selection(self):
        self._table.selection_set('')

    def clear(self):
        iids = self._table.get_children()
        self._table.delete(*iids)
        self._channel_map.clear()
        self.shrink()

    def display(self, channel: Channel, *, add: bool = False):
        iid = channel.iid
        if not add and iid not in self._channel_map:
            # the channel isn't on the list and we're not supposed to add it
            return
        # ACL-based
        acl_based = "✔" if channel.acl_based else "❌"
        # status
        if channel.online:
            status = _("gui", "channels", "online")
        elif channel.pending_online:
            status = _("gui", "channels", "pending")
        else:
            status = _("gui", "channels", "offline")
        # game
        game = str(channel.game or '')
        # drops
        drops = "✔" if channel.drops_enabled else "❌"
        # viewers
        viewers = ''
        if channel.viewers is not None:
            viewers = str(channel.viewers)
        # points
        points = ''
        if channel.points is not None:
            points = str(channel.points)
        if iid in self._channel_map:
            self._set(iid, "game", game)
            self._set(iid, "drops", drops)
            self._set(iid, "status", status)
            self._set(iid, "viewers", viewers)
            self._set(iid, "acl_base", acl_based)
            if points != '':  # we still want to display 0
                self._set(iid, "points", points)
        elif add:
            self._channel_map[iid] = channel
            self._insert(
                iid,
                {
                    "game": game,
                    "drops": drops,
                    "points": points,
                    "status": status,
                    "viewers": viewers,
                    "acl_base": acl_based,
                    "channel": channel.name,
                },
            )

    def remove(self, channel: Channel):
        iid = channel.iid
        del self._channel_map[iid]
        self._table.delete(iid)


class TrayIcon:
    """System tray icon component."""

    TITLE = "Twitch Drops Miner"

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._manager = manager
        self.icon: pystray.Icon | None = None
        self.icon_image = Image_module.open(resource_path("pickaxe.ico"))
        self._button = ttk.Button(master, command=self.minimize, text=_("gui", "tray", "minimize"))
        self._button.grid(column=0, row=0, sticky="ne")
        self.always_show_icon = True        # Ensure there is a way to restore the window position, in case it's shown off-screen (e.g. Second monitor)
        self._started = False

    def __del__(self) -> None:
        self.stop()
        self.icon_image.close()

    def get_title(self, drop: TimedDrop | None) -> str:
        if drop is None:
            return self.TITLE
        campaign = drop.campaign
        title = (
            f"{self.TITLE}\n"
            f"{campaign.game.name}\n"
            f"{drop.rewards_text()} "
            f"{drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        if  len(title) > 127:        # ValueError: string too long (x, maximum length 128), but it only shows 127
            min_length = 30
            diff = len(title) - 127
            if (len(drop.rewards_text()) - diff) >= min_length + 1:     # If we can trim the drop name to 20 chars
                new_length = len(drop.rewards_text()) - diff - 1        # Length - Diff - Ellipsis (…)
                title = (
                    f"{self.TITLE}\n"
                    f"{campaign.game.name}\n"
                    f"{drop.rewards_text()[:new_length]}… "
                    f"{drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
                )
            else:                                                                                               # Trimming both
                new_length = len(campaign.game.name) - (diff - len(drop.rewards_text()) + min_length + 1) - 1   # Campaign name - (Remaining diff from trimmed drop name) - Ellipsis
                title = (
                    f"{self.TITLE}\n"
                    f"{campaign.game.name[:new_length]}…\n"
                    f"{drop.rewards_text()[:min_length]}… "
                    f"{drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
                )
        return title

    def start(self):
        """Start the tray icon (deferred until event loop is running)."""
        if not self._started and self.always_show_icon:
            self._start()
            self._started = True

    def _start(self):
        loop = asyncio.get_running_loop()
        if not self.always_show_icon:
            drop = self._manager.progress._drop

        # we need this because tray icon lives in a separate thread
        def bridge(func):
            return lambda: loop.call_soon_threadsafe(func)

        menu = pystray.Menu(
            pystray.MenuItem(_("gui", "tray", "show"), bridge(self.restore), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_("gui", "tray", "quit"), bridge(self.quit)),
            pystray.MenuItem(f'{_("gui", "tray", "show")} ({_("gui", "inventory", "filter", "refresh")})', bridge(self.restore_position)),
        )
        if self.always_show_icon:
            self.icon = pystray.Icon("twitch_miner", self.icon_image, self.get_title(None), menu)
        else:
            self.icon = pystray.Icon("twitch_miner", self.icon_image, self.get_title(drop), menu)
        # self.icon.run_detached()
        loop.run_in_executor(None, self.icon.run)

    def restore_position(self):
        if not self.always_show_icon:
            if self.icon is not None:
                # self.stop()
                self.icon.visible = False
        self._manager._root.geometry("0x0+0+0")
        self._manager._root.deiconify()

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

    def quit(self):
        self._manager.close()

    def minimize(self):
        if not self.always_show_icon:
            if self.icon is None:
                self._start()
            else:
                self.icon.visible = True
        self._manager._root.withdraw()

    def restore(self):
        if not self.always_show_icon:
            if self.icon is not None:
                # self.stop()
                self.icon.visible = False
        self._manager._root.deiconify()

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> asyncio.Task[None] | None:
        # do nothing if the user disabled notifications
        if not self._manager._twitch.settings.tray_notifications:
            return None
        if self.icon is not None:
            icon = self.icon  # nonlocal scope bind

            async def notifier():
                icon.notify(message, title)
                await asyncio.sleep(duration)
                icon.remove_notification()

            return asyncio.create_task(notifier())
        return None

    def update_title(self, drop: TimedDrop | None):
        if self.icon is not None:
            self.icon.title = self.get_title(drop)


class Notebook:
    """Notebook widget for tab management."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._nb = ttk.Notebook(master)
        self._nb.grid(column=0, row=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # prevent entries from being selected after switching tabs
        self._nb.bind("<<NotebookTabChanged>>", lambda event: manager._root.focus_set())

    def add_tab(self, widget: ttk.Widget, *, name: str, **kwargs):
        kwargs.pop("text", None)
        if "sticky" not in kwargs:
            kwargs["sticky"] = "nsew"
        self._nb.add(widget, text=name, **kwargs)

    def current_tab(self) -> int:
        return self._nb.index("current")

    def add_view_event(self, callback: abc.Callable[[tk.Event[ttk.Notebook]], Any]):
        self._nb.bind("<<NotebookTabChanged>>", callback, True)
