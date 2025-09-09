"""
Tab-specific components for the Twitch Drops Miner GUI.

This module contains components that are specific to individual tabs
like inventory overview, settings panel, and help tab.
"""

from __future__ import annotations

import os
import sys
import asyncio
import tkinter as tk
from pathlib import Path
from collections import abc
from textwrap import dedent
from dataclasses import dataclass
from tkinter.font import Font, nametofont
from functools import partial
from datetime import datetime, timezone
from tkinter import ttk, StringVar, DoubleVar, IntVar
import tkinter.simpledialog
from typing import Any, Union, Tuple, TypedDict, TYPE_CHECKING

from yarl import URL
from PIL.ImageTk import PhotoImage

if sys.platform == "win32":
    from ..registry import RegistryKey, ValueType

from ..translate import _
from ..cache import ImageCache
from ..utils import resource_path, set_root_icon, webopen, Game, _T
from ..constants import (
    SELF_PATH, WINDOW_TITLE, State,
    PRIORITY_ALGORITHM_LIST, PRIORITY_ALGORITHM_ADAPTIVE, PRIORITY_ALGORITHM_BALANCED, PRIORITY_ALGORITHM_ENDING_SOONEST
)
from .widgets import PlaceholderEntry, PlaceholderCombobox, PaddedListbox, MouseOverLabel, LinkLabel, SelectMenu

if TYPE_CHECKING:
    from ..twitch import Twitch
    from ..channel import Channel
    from ..settings import Settings
    from ..inventory import DropsCampaign, TimedDrop
    from .manager import GUIManager


class CampaignDisplay(TypedDict):
    """Campaign display data structure."""
    frame: ttk.Frame
    status: ttk.Label


class InventoryOverview:
    """Inventory overview tab component."""

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._manager = manager
        self._cache: ImageCache = manager._cache
        self._settings: Settings = manager._twitch.settings
        self._filters = {
            "not_linked": IntVar(master, 1),
            "upcoming": IntVar(master, 1),
            "expired": IntVar(master, 0),
            "excluded": IntVar(master, 0),
            "finished": IntVar(master, 0),
        }
        manager.tabs.add_view_event(self._on_tab_switched)
        # Filtering options
        filter_frame = ttk.LabelFrame(
            master, text=_("gui", "inventory", "filter", "name"), padding=(4, 0, 4, 4)
        )
        LABEL_SPACING = 20
        filter_frame.grid(column=0, row=0, columnspan=2, sticky="nsew")
        ttk.Label(
            filter_frame, text=_("gui", "inventory", "filter", "show"), padding=(0, 0, 10, 0)
        ).grid(column=0, row=0)
        icolumn = 0
        ttk.Checkbutton(
            filter_frame, variable=self._filters["not_linked"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "not_linked"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["upcoming"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "upcoming"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["expired"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "expired"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["excluded"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "excluded"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["finished"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "finished"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Button(
            filter_frame, text=_("gui", "inventory", "filter", "refresh"), command=self.refresh
        ).grid(column=(icolumn := icolumn + 1), row=0)
        # Inventory view
        self._canvas = tk.Canvas(master, scrollregion=(0, 0, 0, 0))
        self._canvas.grid(column=0, row=1, sticky="nsew")
        master.rowconfigure(1, weight=1)
        master.columnconfigure(0, weight=1)
        xscroll = ttk.Scrollbar(master, orient="horizontal", command=self._canvas.xview)
        xscroll.grid(column=0, row=2, sticky="ew")
        yscroll = ttk.Scrollbar(master, orient="vertical", command=self._canvas.yview)
        yscroll.grid(column=1, row=1, sticky="ns")
        self._canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self._canvas.bind("<Configure>", self._canvas_update)
        self._main_frame = ttk.Frame(self._canvas)
        self._canvas.bind(
            "<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        )
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self._canvas.create_window(0, 0, anchor="nw", window=self._main_frame)
        self._campaigns: dict[DropsCampaign, CampaignDisplay] = {}
        self._drops: dict[str, MouseOverLabel] = {}

    def _update_visibility(self, campaign: DropsCampaign):
        # True if the campaign is supposed to show, False makes it hidden.
        frame = self._campaigns[campaign]["frame"]
        not_linked = bool(self._filters["not_linked"].get())
        expired = bool(self._filters["expired"].get())
        excluded = bool(self._filters["excluded"].get())
        upcoming = bool(self._filters["upcoming"].get())
        finished = bool(self._filters["finished"].get())
        priority_only = self._settings.priority_only
        if (
            (not_linked or campaign.linked)
            and (campaign.active or upcoming and campaign.upcoming or expired and campaign.expired)
            and (
                excluded or (
                    campaign.game.name not in self._settings.exclude
                    and not priority_only or campaign.game.name in self._settings.priority
                )
            )
            and (finished or not campaign.finished)
        ):
            frame.grid()
        else:
            frame.grid_remove()

    def _on_tab_switched(self, event: tk.Event[ttk.Notebook]) -> None:
        if self._manager.tabs.current_tab() == 1:
            # refresh only if we're switching to the tab
            self.refresh()

    def refresh(self):
        for campaign in self._campaigns:
            # status
            status_label = self._campaigns[campaign]["status"]
            status_text, status_color = self.get_status(campaign)
            status_label.config(text=status_text, foreground=status_color)
            # visibility
            self._update_visibility(campaign)
        self._canvas_update()

    def _canvas_update(self, event: tk.Event[tk.Canvas] | None = None):
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event[tk.Misc]):
        delta = -1 if event.delta > 0 else 1
        state: int = event.state if isinstance(event.state, int) else 0
        if state & 1:
            scroll = self._canvas.xview_scroll
        else:
            scroll = self._canvas.yview_scroll
        scroll(delta, "units")

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        campaign_frame = ttk.Frame(self._main_frame, relief="ridge", borderwidth=1, padding=4)
        campaign_frame.grid(column=0, row=len(self._campaigns), sticky="nsew", pady=3)
        campaign_frame.rowconfigure(4, weight=1)
        campaign_frame.columnconfigure(1, weight=1)
        campaign_frame.columnconfigure(3, weight=10000)
        # Name
        ttk.Label(
            campaign_frame, text=campaign.name, takefocus=False, width=45
        ).grid(column=0, row=0, columnspan=2, sticky="w")
        # Status
        status_text, status_color = self.get_status(campaign)
        status_label = ttk.Label(
            campaign_frame, text=status_text, takefocus=False, foreground=status_color
        )
        status_label.grid(column=1, row=1, sticky="w", padx=4)
        # Starts / Ends
        MouseOverLabel(
            campaign_frame,
            text=_("gui", "inventory", "ends").format(
                time=campaign.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            alt_text=_("gui", "inventory", "starts").format(
                time=campaign.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            reverse=campaign.upcoming,
            takefocus=False,
        ).grid(column=1, row=2, sticky="w", padx=4)
        # Linking status
        if campaign.linked:
            link_kwargs = {
                "style": '',
                "text": _("gui", "inventory", "status", "linked"),
                "foreground": "green",
            }
        else:
            link_kwargs = {
                "text": _("gui", "inventory", "status", "not_linked"),
                "foreground": "red",
            }
        LinkLabel(
            campaign_frame,
            link=campaign.link_url,
            takefocus=False,
            padding=0,
            **link_kwargs,
        ).grid(column=1, row=3, sticky="w", padx=4)
        # ACL channels
        acl = campaign.allowed_channels
        if acl:
            if len(acl) <= 5:
                allowed_text: str = '\n'.join(ch.name for ch in acl)
            else:
                allowed_text = '\n'.join(ch.name for ch in acl[:4])
                allowed_text += (
                    f"\n{_('gui', 'inventory', 'and_more').format(amount=len(acl) - 4)}"
                )
        else:
            allowed_text = _("gui", "inventory", "all_channels")
        ttk.Label(
            campaign_frame,
            text=f"{_('gui', 'inventory', 'allowed_channels')}\n{allowed_text}",
            takefocus=False,
        ).grid(column=1, row=4, sticky="nw", padx=4)
        # Image
        campaign_image = await self._cache.get(campaign.image_url, size=(108, 144))
        ttk.Label(campaign_frame, image=campaign_image).grid(column=0, row=1, rowspan=4)
        # Drops separator
        ttk.Separator(
            campaign_frame, orient="vertical", takefocus=False
        ).grid(column=2, row=0, rowspan=5, sticky="ns")
        # Drops display
        drops_row = ttk.Frame(campaign_frame)
        drops_row.grid(column=3, row=0, rowspan=5, sticky="nsew", padx=4)
        drops_row.rowconfigure(0, weight=1)
        for i, drop in enumerate(campaign.drops):
            drop_frame = ttk.Frame(drops_row, relief="ridge", borderwidth=1, padding=5)
            drop_frame.grid(column=i, row=0, padx=4)
            benefits_frame = ttk.Frame(drop_frame)
            benefits_frame.grid(column=0, row=0)
            benefit_images: list[PhotoImage] = await asyncio.gather(
                *(self._cache.get(benefit.image_url, (80, 80)) for benefit in drop.benefits)
            )
            for i, benefit, image in zip(range(len(drop.benefits)), drop.benefits, benefit_images):
                ttk.Label(
                    benefits_frame, text=benefit.name, image=image, compound="bottom"
                ).grid(column=i, row=0, padx=5)
            self._drops[drop.id] = label = MouseOverLabel(drop_frame)
            self.update_progress(drop, label)
            label.grid(column=0, row=1)
        self._campaigns[campaign] = {
            "frame": campaign_frame,
            "status": status_label,
        }
        if self._manager.tabs.current_tab() == 1:
            self._update_visibility(campaign)
            self._canvas_update()

    def clear(self) -> None:
        for child in self._main_frame.winfo_children():
            child.destroy()
        self._drops.clear()
        self._campaigns.clear()

    def get_status(self, campaign: DropsCampaign) -> tuple[str, str]:
        if campaign.active:
            status_text: str = _("gui", "inventory", "status", "active")
            status_color: str = "green"
        elif campaign.upcoming:
            status_text = _("gui", "inventory", "status", "upcoming")
            status_color = "goldenrod"
        else:
            status_text = _("gui", "inventory", "status", "expired")
            status_color = "red"
        return (status_text, status_color)

    def update_progress(self, drop: TimedDrop, label: MouseOverLabel) -> None:
        # Returns: main text, alt text, text color
        alt_text: str = ''
        progress_text: str
        reverse: bool = False
        progress_color: str = ''
        if drop.is_claimed:
            progress_color = "green"
            progress_text = _("gui", "inventory", "status", "claimed")
        elif drop.can_claim:
            progress_color = "goldenrod"
            progress_text = _("gui", "inventory", "status", "ready_to_claim")
        elif drop.current_minutes or drop.can_earn():
            progress_text = _("gui", "inventory", "percent_progress").format(
                percent=f"{drop.progress:3.1%}",
                minutes=drop.required_minutes,
            )
        else:
            progress_text = _("gui", "inventory", "minutes_progress").format(
                minutes=drop.required_minutes
            )
            if datetime.now(timezone.utc) < drop.starts_at > drop.campaign.starts_at:
                # this drop can only be earned later than the campaign start
                alt_text = _("gui", "inventory", "starts").format(
                    time=drop.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
                reverse = True
            elif drop.ends_at < drop.campaign.ends_at:
                # this drop becomes unavailable earlier than the campaign ends
                alt_text = _("gui", "inventory", "ends").format(
                    time=drop.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
                reverse = True
        label.config(
            text=progress_text, alt_text=alt_text, reverse=reverse, foreground=progress_color
        )

    def update_drop(self, drop: TimedDrop) -> None:
        label = self._drops.get(drop.id)
        if label is None:
            return
        self.update_progress(drop, label)


def proxy_validate(entry: PlaceholderEntry, settings: Settings) -> bool:
    """Validate proxy URL entry."""
    raw_url = entry.get().strip()
    entry.replace(raw_url)
    url = URL(raw_url)
    valid = url.host is not None and url.port is not None
    if not valid:
        entry.clear()
        url = URL()
    settings.proxy = url
    return valid


class _SettingsVars(TypedDict):
    """Settings variables data structure."""
    tray: IntVar
    proxy: StringVar
    dark_theme: IntVar
    autostart: IntVar
    priority_only: IntVar
    priority_algorithm: StringVar
    unlinked_campaigns: IntVar
    tray_notifications: IntVar
    window_position: StringVar


class SettingsPanel:
    """Settings panel tab component."""

    AUTOSTART_NAME: str = "TwitchDropsMiner"
    AUTOSTART_KEY: str = "HKCU/Software/Microsoft/Windows/CurrentVersion/Run"

    def __init__(self, manager: 'GUIManager', master: ttk.Widget, root: tk.Tk):
        self._manager = manager
        self._root = root
        self._twitch = manager._twitch
        self._settings: Settings = manager._twitch.settings
        self._vars: _SettingsVars = {
            "proxy": StringVar(master, str(self._settings.proxy)),
            "tray": IntVar(master, self._settings.autostart_tray),
            "dark_theme": IntVar(master, self._settings.dark_theme),
            "autostart": IntVar(master, self._settings.autostart),
            "priority_only": IntVar(master, self._settings.priority_only),
            "priority_algorithm": StringVar(master, self._settings.priority_algorithm),
            "unlinked_campaigns": IntVar(master, self._settings.unlinked_campaigns),
            "tray_notifications": IntVar(master, self._settings.tray_notifications),
            "window_position": IntVar(master, self._settings.window_position),
        }
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # use a frame to fill the content within the tab
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0, sticky="nsew")
        center_frame.rowconfigure(0, weight=1)
        # Simple equal column layout
        center_frame.columnconfigure(0, weight=1)
        center_frame.columnconfigure(1, weight=1)
        center_frame.columnconfigure(2, weight=1)
        # General section
        general_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "general", "name")
        )
        general_frame.grid(column=0, row=0, sticky="nsew")
        # use another frame to contain the options within the section
        general_frame.rowconfigure(0, weight=1)
        general_frame.columnconfigure(0, weight=1)
        center_frame2 = ttk.Frame(general_frame)
        center_frame2.grid(column=0, row=0, sticky="nsew")
        # language frame
        language_frame = ttk.Frame(center_frame2)
        language_frame.grid(column=0, row=0)
        ttk.Label(language_frame, text=_("gui", "settings", "general", "language")).grid(column=0, row=0)
        self._select_menu = SelectMenu(
            language_frame,
            default=_.current,
            options={k: k for k in _.languages},
            command=lambda lang: setattr(self._settings, "language", lang),
        )
        self._select_menu.grid(column=1, row=0)
        # checkboxes frame
        checkboxes_frame = ttk.Frame(center_frame2)
        checkboxes_frame.grid(column=0, row=1)
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "dark_theme")
        ).grid(column=0, row=(irow := 0), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["dark_theme"], command=self.change_theme
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "autostart")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["autostart"], command=self.update_autostart
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "tray")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["tray"], command=self.update_autostart
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "tray_notifications")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame,
            variable=self._vars["tray_notifications"],
            command=self.update_notifications,
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "priority_only")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["priority_only"], command=self.priority_only
        ).grid(column=1, row=irow, sticky="w")
        # Priority algorithm selection frame
        priority_algorithm_frame = ttk.Frame(center_frame2)
        priority_algorithm_frame.grid(column=0, row=2)
        ttk.Label(priority_algorithm_frame, text=_("gui", "settings", "general", "priority_algorithm")).grid(column=0, row=0, sticky="e")
        # Map setting values to display names
        algorithm_display_map = {
            PRIORITY_ALGORITHM_LIST: _("gui", "settings", "general", "priority_algorithms", "list"),
            PRIORITY_ALGORITHM_ADAPTIVE: _("gui", "settings", "general", "priority_algorithms", "adaptive"),
            PRIORITY_ALGORITHM_BALANCED: _("gui", "settings", "general", "priority_algorithms", "balanced"),
            PRIORITY_ALGORITHM_ENDING_SOONEST: _("gui", "settings", "general", "priority_algorithms", "ending_soonest"),
        }
        # Ensure we always have a valid algorithm setting and display name
        current_algorithm = getattr(self._settings, 'priority_algorithm', PRIORITY_ALGORITHM_LIST)
        current_algorithm_display = algorithm_display_map.get(current_algorithm,  _("gui", "settings", "general", "priority_algorithms", "list"))

        # If setting is invalid, reset it to default
        if current_algorithm not in algorithm_display_map:
            self._settings.priority_algorithm = PRIORITY_ALGORITHM_LIST
            current_algorithm_display = _("gui", "settings", "general", "priority_algorithms", "list")

        self._priority_algorithm_menu = SelectMenu(
            priority_algorithm_frame,
            default=current_algorithm_display,
            options={
                _("gui", "settings", "general", "priority_algorithms", "list"): PRIORITY_ALGORITHM_LIST,
                _("gui", "settings", "general", "priority_algorithms", "adaptive"): PRIORITY_ALGORITHM_ADAPTIVE,
                _("gui", "settings", "general", "priority_algorithms", "balanced"): PRIORITY_ALGORITHM_BALANCED,
                _("gui", "settings", "general", "priority_algorithms", "ending_soonest"): PRIORITY_ALGORITHM_ENDING_SOONEST,
            },
            command=self.update_priority_algorithm,
        )
        self._priority_algorithm_menu.grid(column=1, row=0, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "unlinked_campaigns")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["unlinked_campaigns"], command=self.unlinked_campaigns
        ).grid(column=1, row=irow, sticky="w")
        # proxy frame
        proxy_frame = ttk.Frame(center_frame2)
        proxy_frame.grid(column=0, row=3)
        ttk.Label(proxy_frame, text=_("gui", "settings", "general", "proxy")).grid(column=0, row=0)
        self._proxy = PlaceholderEntry(
            proxy_frame,
            width=37,
            validate="focusout",
            prefill="http://",
            textvariable=self._vars["proxy"],
            placeholder="http://username:password@address:port",
        )
        self._proxy.config(validatecommand=partial(proxy_validate, self._proxy, self._settings))
        self._proxy.grid(column=0, row=1)
        # Priority section - with width constraint
        priority_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "priority")
        )
        priority_frame.grid(column=1, row=0, sticky="nsew")
        self._priority_entry = PlaceholderCombobox(
            priority_frame, placeholder=_("gui", "settings", "game_name"), width=30
        )
        self._priority_entry.grid(column=0, row=0, sticky="ew")
        priority_frame.columnconfigure(0, weight=1)
        ttk.Button(
            priority_frame, text="+", command=self.priority_add, width=2, style="Large.TButton"
        ).grid(column=1, row=0)
        self._priority_list = PaddedListbox(
            priority_frame,
            height=10,
            padding=(1, 0),
            activestyle="none",
            selectmode="single",
            highlightthickness=0,
            exportselection=False,
        )
        self._priority_list.grid(column=0, row=1, rowspan=3, sticky="nsew")
        self._priority_list.insert("end", *self._settings.priority)

        # Add drag and drop functionality with visual feedback
        self._drag_data = {"item": None, "index": None, "dragging": False}
        self._priority_list.bind("<Button-1>", self._on_priority_drag_start)
        self._priority_list.bind("<B1-Motion>", self._on_priority_drag_motion)
        self._priority_list.bind("<ButtonRelease-1>", self._on_priority_drag_release)
        self._priority_list.bind("<Leave>", self._on_priority_drag_leave)

        # Add a label for drag feedback (initially hidden)
        self._drag_feedback_label = ttk.Label(priority_frame, text="",
                                            foreground="blue", font=("TkDefaultFont", 9, "italic"))
        self._drag_feedback_label.grid(column=0, row=4, columnspan=2, sticky="ew")
        self._drag_feedback_label.grid_remove()  # Hide initially

        # Add right-click context menu
        self._priority_menu = tk.Menu(self._priority_list, tearoff=0)
        self._priority_menu.add_command(label=_("gui", "settings", "context_menu", "move_to_top"), command=lambda: self._priority_move_to_position(0))
        self._priority_menu.add_command(label=_("gui", "settings", "context_menu", "move_to_bottom"), command=lambda: self._priority_move_to_position(-1))
        self._priority_menu.add_separator()
        self._priority_menu.add_command(label=_("gui", "settings", "context_menu", "move_to_position"), command=self._priority_move_to_custom_position)
        self._priority_list.bind("<Button-3>", self._show_priority_context_menu)  # Right-click
        ttk.Button(
            priority_frame,
            width=2,
            text="▲",
            style="Large.TButton",
            command=partial(self.priority_move, True),
        ).grid(column=1, row=1, sticky="ns")
        priority_frame.rowconfigure(1, weight=1)
        ttk.Button(
            priority_frame,
            width=2,
            text="▼",
            style="Large.TButton",
            command=partial(self.priority_move, False),
        ).grid(column=1, row=2, sticky="ns")
        priority_frame.rowconfigure(2, weight=1)
        ttk.Button(
            priority_frame, text="❌", command=self.priority_delete, width=2, style="Large.TButton"
        ).grid(column=1, row=3, sticky="ns")
        priority_frame.rowconfigure(3, weight=1)
        # Exclude section - with width constraint
        exclude_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "exclude")
        )
        exclude_frame.grid(column=2, row=0, sticky="nsew")
        self._exclude_entry = PlaceholderCombobox(
            exclude_frame, placeholder=_("gui", "settings", "game_name"), width=26
        )
        self._exclude_entry.grid(column=0, row=0, sticky="ew")
        ttk.Button(
            exclude_frame, text="+", command=self.exclude_add, width=2, style="Large.TButton"
        ).grid(column=1, row=0)
        self._exclude_list = PaddedListbox(
            exclude_frame,
            height=10,
            padding=(1, 0),
            activestyle="none",
            selectmode="single",
            highlightthickness=0,
            exportselection=False,
        )
        self._exclude_list.grid(column=0, row=1, columnspan=2, sticky="nsew")
        exclude_frame.rowconfigure(1, weight=1)
        # insert them alphabetically
        self._exclude_list.insert("end", *sorted(self._settings.exclude))
        ttk.Button(
            exclude_frame, text="❌", command=self.exclude_delete, width=2, style="Large.TButton"
        ).grid(column=0, row=2, columnspan=2, sticky="ew")
        # Reload button
        reload_frame = ttk.Frame(center_frame)
        reload_frame.grid(column=0, row=1, columnspan=3, pady=4)
        ttk.Label(reload_frame, text=_("gui", "settings", "reload_text")).grid(column=0, row=0)
        ttk.Button(
            reload_frame,
            text=_("gui", "settings", "reload"),
            command=self._twitch.state_change(State.INVENTORY_FETCH),
        ).grid(column=1, row=0)

    def clear_selection(self) -> None:
        self._priority_list.selection_clear(0, "end")
        self._exclude_list.selection_clear(0, "end")

    def update_notifications(self) -> None:
        self._settings.tray_notifications = bool(self._vars["tray_notifications"].get())

    def _get_autostart_path(self, tray: bool) -> str:
        self_path = f'"{SELF_PATH.resolve()!s}"'
        if tray:
            self_path += " --tray"
        return self_path

    def change_theme(self):
        self._settings.dark_theme = bool(self._vars["dark_theme"].get())
        if self._settings.dark_theme:
            from .manager import set_theme
            set_theme(self._root, self._manager, _("gui", "settings", "general", "dark"))
        else:
            from .manager import set_theme
            set_theme(self._root, self._manager, _("gui", "settings", "general", "light"))

    def update_autostart(self) -> None:
        enabled = bool(self._vars["autostart"].get())
        tray = bool(self._vars["tray"].get())
        self._settings.autostart = enabled
        self._settings.autostart_tray = tray
        if sys.platform == "win32":
            if enabled:
                # NOTE: we need double quotes in case the path contains spaces
                autostart_path = self._get_autostart_path(tray)
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.set(self.AUTOSTART_NAME, ValueType.REG_SZ, autostart_path)
            else:
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.delete(self.AUTOSTART_NAME, silent=True)
        elif sys.platform == "linux":
            autostart_folder: Path = Path("~/.config/autostart").expanduser()
            if (config_home := os.environ.get("XDG_CONFIG_HOME")) is not None:
                config_autostart: Path = Path(config_home, "autostart").expanduser()
                if config_autostart.exists():
                    autostart_folder = config_autostart
            autostart_file: Path = autostart_folder / f"{self.AUTOSTART_NAME}.desktop"
            if enabled:
                autostart_path = self._get_autostart_path(tray)
                file_contents = dedent(
                    f"""
                    [Desktop Entry]
                    Type=Application
                    Name=Twitch Drops Miner
                    Description=Mine timed drops on Twitch
                    Exec=sh -c '{autostart_path}'
                    """
                )
                with autostart_file.open("w", encoding="utf8") as file:
                    file.write(file_contents)
            else:
                autostart_file.unlink(missing_ok=True)

    def set_games(self, games: abc.Iterable[Game]) -> None:
        games_list = sorted(map(str, games))
        # Filter out games that are in the other list to prevent conflicts
        priority_games = set(self._settings.priority)
        exclude_games = set(self._settings.exclude)

        # For exclusion dropdown, exclude games that are in priority list OR already in exclusion list
        exclude_options = [game for game in games_list if game not in priority_games and game not in exclude_games]
        self._exclude_entry.config(values=exclude_options)

        # For priority dropdown, exclude games that are in exclusion list OR already in priority list
        priority_options = [game for game in games_list if game not in exclude_games and game not in priority_games]
        self._priority_entry.config(values=priority_options)

    def _update_dropdown_options(self) -> None:
        """Update dropdown options to exclude games that are in the other list or already in their own list."""
        # Get current dropdown values
        current_exclude_values = list(self._exclude_entry.cget("values"))
        current_priority_values = list(self._priority_entry.cget("values"))

        # Combine all available games
        all_games = sorted(set(current_exclude_values + current_priority_values +
                             list(self._settings.priority) + list(self._settings.exclude)))

        # Filter out games that are in the other list or already in their own list
        priority_games = set(self._settings.priority)
        exclude_games = set(self._settings.exclude)

        # For exclusion dropdown, exclude games that are in priority list OR already in exclusion list
        exclude_options = [game for game in all_games if game not in priority_games and game not in exclude_games]
        self._exclude_entry.config(values=exclude_options)

        # For priority dropdown, exclude games that are in exclusion list OR already in priority list
        priority_options = [game for game in all_games if game not in exclude_games and game not in priority_games]
        self._priority_entry.config(values=priority_options)

    def priorities(self) -> dict[str, int]:
        # NOTE: we shift the indexes so that 0 can be used as the default one
        size = self._priority_list.size()
        return {
            game_name: size - i for i, game_name in enumerate(self._priority_list.get(0, "end"))
        }

    def priority_add(self) -> None:
        game_name: str = self._priority_entry.get()
        if not game_name:
            # prevent adding empty strings
            return
        self._priority_entry.clear()
        # add it preventing duplicates
        try:
            existing_idx: int = self._settings.priority.index(game_name)
        except ValueError:
            # not there, add it
            self._priority_list.insert("end", game_name)
            self._priority_list.see("end")
            self._settings.priority.append(game_name)
            self._settings.alter()
            # Update dropdown options to reflect the change
            self._update_dropdown_options()
        else:
            # already there, set the selection on it
            self._priority_list.selection_set(existing_idx)
            self._priority_list.see(existing_idx)

    def _priority_idx(self) -> int | None:
        selection: tuple[int, ...] = self._priority_list.curselection()
        if not selection:
            return None
        return selection[0]

    def priority_move(self, up: bool) -> None:
        current_selection = list(self._priority_list.curselection())
        if not current_selection:
            return

        idx = current_selection[0]
        if up and idx == 0 or not up and idx == self._priority_list.size() - 1:
            return
        swap_idx: int = idx - 1 if up else idx + 1
        item: str = self._priority_list.get(idx)
        self._priority_list.delete(idx)
        self._priority_list.insert(swap_idx, item)
        # reselect the item and scroll the list if needed
        self._priority_list.selection_set(swap_idx)
        self._priority_list.see(swap_idx)
        p = self._settings.priority
        p[idx], p[swap_idx] = p[swap_idx], p[idx]
        self._settings.alter()

    def priority_delete(self) -> None:
        current_selection = list(self._priority_list.curselection())
        if not current_selection:
            return

        idx = current_selection[0]
        if idx < len(self._settings.priority):
            self._priority_list.delete(idx)
            del self._settings.priority[idx]

        self._settings.alter()
        # Update dropdown options to reflect the change
        self._update_dropdown_options()

    def priority_only(self) -> None:
        self._settings.priority_only = bool(self._vars["priority_only"].get())

    def update_priority_algorithm(self, algorithm: str) -> None:
        self._settings.priority_algorithm = algorithm

    def unlinked_campaigns(self) -> None:
        self._settings.unlinked_campaigns = bool(self._vars["unlinked_campaigns"].get())

    def exclude_add(self) -> None:
        game_name: str = self._exclude_entry.get()
        if not game_name:
            # prevent adding empty strings
            return
        self._exclude_entry.clear()
        exclude = self._settings.exclude
        if game_name not in exclude:
            exclude.add(game_name)
            self._settings.alter()
            # insert it alphabetically
            for i, item in enumerate(self._exclude_list.get(0, "end")):
                if game_name < item:
                    self._exclude_list.insert(i, game_name)
                    self._exclude_list.see(i)
                    break
            else:
                self._exclude_list.insert("end", game_name)
                self._exclude_list.see("end")
            # Update dropdown options to reflect the change
            self._update_dropdown_options()
        else:
            # it was already there, select it
            for i, item in enumerate(self._exclude_list.get(0, "end")):
                if item == game_name:
                    existing_idx = i
                    break
            else:
                # something went horribly wrong and it's not there after all - just return
                return
            self._exclude_list.selection_set(existing_idx)
            self._exclude_list.see(existing_idx)

    def exclude_delete(self) -> None:
        selection: tuple[int, ...] = self._exclude_list.curselection()
        if not selection:
            return None
        idx: int = selection[0]
        item: str = self._exclude_list.get(idx)
        if item in self._settings.exclude:
            self._settings.exclude.discard(item)
            self._settings.alter()
            self._exclude_list.delete(idx)
            # Update dropdown options to reflect the change
            self._update_dropdown_options()

    # Drag and drop methods for priority list with enhanced visual feedback
    def _on_priority_drag_start(self, event):
        """Start drag operation - record the item being dragged."""
        widget = event.widget
        clicked_index = widget.nearest(event.y)

        if clicked_index < widget.size():
            # Select the clicked item
            widget.selection_clear(0, "end")
            widget.selection_set(clicked_index)

            # Store drag data for the single item
            self._drag_data["index"] = clicked_index
            self._drag_data["item"] = widget.get(clicked_index)
            self._drag_data["dragging"] = False

            # Configure drag source appearance
            widget.configure(cursor="hand2")

    def _on_priority_drag_motion(self, event):
        """Handle drag motion - provide rich visual feedback."""
        widget = event.widget
        current_index = widget.nearest(event.y)

        # Only handle motion if we have drag data
        if self._drag_data["item"]:
            # Mark that we're actively dragging
            if not self._drag_data["dragging"]:
                self._drag_data["dragging"] = True
                widget.configure(cursor="exchange")
                # Show drag feedback
                feedback_text =  _("gui", "settings", "general", "dragging").format(
                    item=self._drag_data['item']
                )
                self._drag_feedback_label.config(text=feedback_text)
                self._drag_feedback_label.grid()

            # Clear all previous selections and highlights
            widget.selection_clear(0, "end")

            # Highlight the source item being dragged
            widget.selection_set(self._drag_data["index"])

            # Highlight the current drop target position if it's different from source
            if current_index < widget.size() and current_index != self._drag_data["index"]:
                widget.selection_set(current_index)

    def _on_priority_drag_release(self, event):
        """Complete drag operation - move the items to new position."""
        widget = event.widget
        drop_index = widget.nearest(event.y)

        # Reset cursor
        widget.configure(cursor="")

        # Only move if we were actually dragging and it's a valid move
        if (self._drag_data["dragging"] and
            self._drag_data["item"] and
            drop_index < widget.size() and
            drop_index != self._drag_data["index"]):

            # Perform the single-item move operation
            self._priority_move_item(self._drag_data["index"], drop_index)
        else:
            # Just clean up selection if no move occurred
            widget.selection_clear(0, "end")
            if self._drag_data["index"] is not None and self._drag_data["index"] < widget.size():
                widget.selection_set(self._drag_data["index"])

        # Clear drag data and hide feedback
        self._drag_data = {"item": None, "index": None, "dragging": False}
        self._drag_feedback_label.grid_remove()

    def _on_priority_drag_leave(self, event):
        """Handle when mouse leaves the listbox during drag."""
        widget = event.widget
        if self._drag_data["dragging"]:
            # Reset cursor and clear highlights when leaving
            widget.configure(cursor="no")
            widget.selection_clear(0, "end")
            # Keep only the source item selected
            if self._drag_data["index"] is not None and self._drag_data["index"] < widget.size():
                widget.selection_set(self._drag_data["index"])
            # Update feedback to show invalid drop zone
            self._drag_feedback_label.config(text="❌ Invalid drop zone - return to list")

    def _priority_move_item(self, from_index: int, to_index: int):
        """Move an item from one position to another in the priority list."""
        if from_index == to_index:
            return

        # Get the item being moved
        item = self._priority_list.get(from_index)

        # Remove from old position
        self._priority_list.delete(from_index)
        self._settings.priority.pop(from_index)

        # Insert at new position
        self._priority_list.insert(to_index, item)
        self._settings.priority.insert(to_index, item)

        # Update selection and ensure visibility
        self._priority_list.selection_clear(0, "end")
        self._priority_list.selection_set(to_index)
        self._priority_list.see(to_index)

        # Save changes
        self._settings.alter()

    # Context menu methods for priority list
    def _show_priority_context_menu(self, event):
        """Show the right-click context menu for priority list."""
        widget = event.widget
        index = widget.nearest(event.y)
        if index < widget.size():
            # Select the clicked item
            widget.selection_clear(0, "end")
            widget.selection_set(index)
            # Show context menu
            try:
                self._priority_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._priority_menu.grab_release()

    def _priority_move_to_position(self, position: int):
        """Move selected item to a specific position (0=top, -1=bottom)."""
        current_selection = list(self._priority_list.curselection())
        if not current_selection:
            return

        list_size = self._priority_list.size()
        if position == -1:  # Move to bottom
            target_idx = list_size - 1
        else:  # Move to specific position
            target_idx = max(0, min(position, list_size - 1))

        self._priority_move_item(current_selection[0], target_idx)

    def _priority_move_by_offset(self, offset: int):
        """Move selected item by a relative offset (negative=up, positive=down)."""
        current_selection = list(self._priority_list.curselection())
        if not current_selection:
            return

        current_idx = current_selection[0]
        new_idx = current_idx + offset
        list_size = self._priority_list.size()
        target_idx = max(0, min(new_idx, list_size - 1))

        if target_idx != current_idx:
            self._priority_move_item(current_idx, target_idx)

    def _priority_move_to_custom_position(self):
        """Show dialog to move item to a custom position."""
        current_selection = list(self._priority_list.curselection())
        if not current_selection:
            return

        list_size = self._priority_list.size()
        current_idx = current_selection[0]

        # Create a simple dialog for position input
        prompt = _("gui", "settings", "context_menu", "enter_position").format(
            list_size=list_size
        )
        initial_value = current_idx + 1

        position = tkinter.simpledialog.askinteger(
            _("gui", "settings", "context_menu", "move_to_position"),
            prompt,
            minvalue=1,
            maxvalue=list_size,
            initialvalue=initial_value
        )

        if position is not None:
            # Convert from 1-based to 0-based indexing
            target_idx = position - 1
            self._priority_move_item(current_idx, target_idx)


class HelpTab:
    """Help tab component."""

    WIDTH = 800

    def __init__(self, manager: 'GUIManager', master: ttk.Widget):
        self._twitch = manager._twitch
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # use a frame to center the content within the tab
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0)
        irow = 0
        # About
        about = ttk.LabelFrame(center_frame, padding=(4, 0, 4, 4), text="About")
        about.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        about.columnconfigure(2, weight=1)
        # About - created by
        ttk.Label(
            about, text="Application created by: ", anchor="e"
        ).grid(column=0, row=0, sticky="nsew")
        LinkLabel(
            about, link="https://github.com/DevilXD", text="DevilXD"
        ).grid(column=1, row=0, sticky="nsew")
        # About - repo link
        ttk.Label(about, text="Repository: ", anchor="e").grid(column=0, row=1, sticky="nsew")
        LinkLabel(
            about,
            link="https://github.com/DevilXD/TwitchDropsMiner",
            text="https://github.com/DevilXD/TwitchDropsMiner",
        ).grid(column=1, row=1, sticky="nsew")
        # About - donate
        ttk.Separator(
            about, orient="horizontal"
        ).grid(column=0, row=2, columnspan=3, sticky="nsew")
        ttk.Label(about, text="Donate: ", anchor="e").grid(column=0, row=3, sticky="nsew")
        LinkLabel(
            about,
            link="https://www.buymeacoffee.com/DevilXD",
            text=(
                "If you like the application and found it useful, "
                "please consider donating a small amount of money to support me. Thank you!"
            ),
            wraplength=self.WIDTH,
        ).grid(column=1, row=3, sticky="nsew")
        # Useful links
        links = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "links", "name")
        )
        links.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/inventory",
            text=_("gui", "help", "links", "inventory"),
        ).grid(column=0, row=0, sticky="nsew")
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/campaigns",
            text=_("gui", "help", "links", "campaigns"),
        ).grid(column=0, row=1, sticky="nsew")
        # How It Works
        howitworks = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "how_it_works")
        )
        howitworks.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            howitworks, text=_("gui", "help", "how_it_works_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")
        getstarted = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "getting_started")
        )
        getstarted.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            getstarted, text=_("gui", "help", "getting_started_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")
