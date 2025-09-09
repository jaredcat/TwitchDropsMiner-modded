"""
Twitch module for Twitch Drops Miner.

This module contains all Twitch-related functionality organized into logical submodules:
- auth: Authentication handling
- client: Main Twitch client class
- drops: Drops and campaign management
- streams: Stream watching and channel management
- websocket: WebSocket message handling
"""

from .client import Twitch
from .auth import _AuthState
from .drops import DropsManager
from .streams import StreamsManager
from .websocket import WebsocketManager, Websocket, WebsocketPool

__all__ = [
    "Twitch",
    "_AuthState",
    "DropsManager",
    "StreamsManager",
    "WebsocketManager",
    "Websocket",
    "WebsocketPool",
]
