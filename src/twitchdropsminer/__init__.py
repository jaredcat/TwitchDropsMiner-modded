"""
Twitch Drops Miner - A tool for automatically farming Twitch drops.

This package provides functionality to automatically watch Twitch streams
and collect drops for various games and campaigns.
"""

__version__ = "1.0.0"
__author__ = "TwitchDropsMiner Team"

# Import main classes for easy access
from .app import run_app
from .twitch import Twitch
from .gui import GUIManager

def main():
    """Main entry point function."""
    run_app()

__all__ = [
    "main",
    "run_app",
    "Twitch",
    "GUIManager",
]
