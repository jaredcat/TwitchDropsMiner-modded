"""
GUI module for Twitch Drops Miner.

This module contains all GUI-related components organized into logical submodules:
- widgets: Basic custom Tkinter widgets
- components: Complex UI components and forms
- tabs: Tab-specific components (inventory, settings, help)
- manager: Main GUI manager and theme handling
"""

from .manager import GUIManager
from .widgets import (
    PlaceholderEntry,
    PlaceholderCombobox,
    PaddedListbox,
    MouseOverLabel,
    LinkLabel,
    SelectMenu,
)
from .components import (
    StatusBar,
    WebsocketStatus,
    LoginForm,
    LoginData,
    CampaignProgress,
    ConsoleOutput,
    ChannelList,
    TrayIcon,
    Notebook,
)
from .tabs import (
    InventoryOverview,
    SettingsPanel,
    HelpTab,
)

__all__ = [
    # Manager
    "GUIManager",
    # Widgets
    "PlaceholderEntry",
    "PlaceholderCombobox",
    "PaddedListbox",
    "MouseOverLabel",
    "LinkLabel",
    "SelectMenu",
    # Components
    "StatusBar",
    "WebsocketStatus",
    "LoginForm",
    "LoginData",
    "CampaignProgress",
    "ConsoleOutput",
    "ChannelList",
    "TrayIcon",
    "Notebook",
    # Tabs
    "InventoryOverview",
    "SettingsPanel",
    "HelpTab",
]
