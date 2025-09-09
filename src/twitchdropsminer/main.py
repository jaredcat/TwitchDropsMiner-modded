"""
Main entry point for Twitch Drops Miner.

This module provides the main entry point and delegates to the app module
for the actual application logic.
"""

from __future__ import annotations

# Import the main application logic
from .app import run_app

if __name__ == "__main__":
    run_app()
