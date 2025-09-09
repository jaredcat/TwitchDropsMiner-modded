"""
Stream watching and channel management for Twitch Drops Miner.

This module handles all stream-related functionality including:
- Channel management and watching
- Stream state processing
- Channel switching logic
- Live stream discovery
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from time import time
from typing import TYPE_CHECKING, Final

from ..translate import _
from ..channel import Channel
from ..exceptions import MinerException
from ..utils import OrderedSet
from ..constants import (
    CALL,
    MAX_CHANNELS,
    WATCH_INTERVAL,
    State,
    WebsocketTopic,
    GQL_OPERATIONS,
)

if TYPE_CHECKING:
    from .client import Twitch
    from .auth import _AuthState
    from ..constants import JsonType
    from ..utils import Game


class StreamsManager:
    """Manages stream watching and channel functionality."""

    def __init__(self, twitch: Twitch):
        self.twitch = twitch
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: Channel | None = None
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        self._drop_update: asyncio.Future[bool] | None = None

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        """Get viewer count for channel sorting."""
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        if not self.twitch.wanted_games:
            return False
        # exit early if
        if (
            not channel.online  # stream is offline
            # or not channel.drops_enabled  # drops aren't enabled
            # there's no game or it's not one of the games we've selected
            or (game := channel.game) is None or game not in self.twitch.wanted_games
        ):
            return False
        # check if we can progress any campaign for the played game
        for campaign in self.twitch.drops.inventory:
            if campaign.game == game and campaign.can_earn(channel):
                return True
        return False

    def should_switch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a switch candidate.
        """
        watching_channel = self.watching_channel.get_with_default(None)
        if watching_channel is None:
            return True
        channel_order = self.twitch.get_priority(channel)
        watching_order = self.twitch.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order > watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        """Start watching a channel."""
        self.twitch.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        if update_status:
            status_text = _("status", "watching").format(channel=channel.name)
            self.twitch.print(status_text)
            self.twitch.gui.status.update(status_text)

    def stop_watching(self):
        """Stop watching the current channel."""
        self.twitch.gui.clear_drop()
        self.watching_channel.clear()
        self.twitch.gui.channels.clear_watching()

    def restart_watching(self):
        """Restart the watching process."""
        self.twitch.gui.progress.stop_timer()
        self._watching_restart.set()

    async def _watch_sleep(self, delay: float) -> None:
        """Sleep with ability to be interrupted by restart."""
        # we use wait_for here to allow an asyncio.sleep-like that can be ended prematurely
        self._watching_restart.clear()
        try:
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _watch_loop(self) -> None:
        """Main watching loop."""
        interval: float = WATCH_INTERVAL.total_seconds()
        while True:
            channel: Channel = await self.watching_channel.get()
            succeeded, repeat_now = await channel.send_watch()
            logger = logging.getLogger("TwitchDrops")
            logger.log(CALL, f"returned watch, succeeded: {succeeded}, repeat_new: {repeat_now}")
            if not succeeded:
                # this usually means the campaign expired in the middle of mining
                # or the m3u8 playlists all returned a 500 Internal server error
                # NOTE: the maintenance task should switch the channel right after this happens
                if not repeat_now:
                    await self._watch_sleep(interval)
                continue
            last_watch = time()
            self._drop_update = asyncio.Future()
            use_active: bool = False
            try:
                handled: bool = await asyncio.wait_for(self._drop_update, timeout=10)
            except asyncio.TimeoutError:
                # there was no websocket update within 10s
                handled = False
                use_active = True
                logger.log(CALL, "No drop update from the websocket received")
            self._drop_update = None
            if not handled:
                # websocket update timed out, or the update was for an unrelated drop
                if not use_active:
                    # we need to use GQL to get the current progress
                    context = await self.twitch.gql_request(GQL_OPERATIONS["CurrentDrop"])
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                    if drop_data is not None:
                        drop = self.twitch.drops.get_drop(drop_data["dropID"])
                        if drop is None:
                            use_active = True
                            # usually this means there was a campaign changed between reloads
                            logger.info("Missing drop detected, reloading...")
                            self.twitch.change_state(State.INVENTORY_FETCH)
                        elif not drop.can_earn(channel):
                            # we can't earn this drop in the current watching channel
                            use_active = True
                            drop_text = (
                                f"{drop.name} ({drop.campaign.game}, "
                                f"{drop.current_minutes}/{drop.required_minutes})"
                            )
                            logger.log(CALL, f"Current drop returned mismach: {drop_text}")
                        else:
                            drop.update_minutes(drop_data["currentMinutesWatched"])
                            drop.display()
                            drop_text = (
                                f"{drop.name} ({drop.campaign.game}, "
                                f"{drop.current_minutes}/{drop.required_minutes})"
                            )
                            logger.log(CALL, f"Drop progress from GQL: {drop_text}")
                    else:
                        use_active = True
                        logger.log(CALL, "Current drop returned as none")
                if use_active:
                    # Sometimes, even GQL fails to give us the correct drop.
                    # In that case, we can use the locally cached inventory to try
                    # and put together the drop that we're actually mining right now
                    # NOTE: get_active_drop uses the watching channel by default,
                    # so there's no point to pass it here
                    if (drop := self.twitch.drops.get_active_drop()) is not None:
                        from cache import CurrentSeconds
                        current_seconds = CurrentSeconds.get_current_seconds()
                        if current_seconds < 1:
                            drop.bump_minutes()
                            drop.display()
                            drop_text = (
                                f"{drop.name} ({drop.campaign.game}, "
                                f"{drop.current_minutes}/{drop.required_minutes})"
                            )
                            logger.log(CALL, f"Drop progress from active search: {drop_text}")
                    else:
                        logger.log(CALL, "No active drop could be determined")
            await self._watch_sleep(last_watch + interval - time())

    async def get_live_streams(self, game: Game, *, limit: int = 30) -> list[Channel]:
        """Get live streams for a specific game."""
        try:
            response = await self.twitch.gql_request(
                GQL_OPERATIONS["GameDirectory"].with_variables({
                    "limit": limit,
                    "slug": game.slug,
                    "options": {
                        "includeRestricted": ["SUB_ONLY_LIVE"],
                        "systemFilters": ["DROPS_ENABLED"],
                    },
                })
            )
        except MinerException as exc:
            raise MinerException(f"Game: {game.slug}") from exc
        if "game" in response["data"]:
            streams = []
            for stream_channel_data in response["data"]["game"]["streams"]["edges"]:
                if stream_channel_data["node"]["broadcaster"]:
                    streams.append(Channel.from_directory(self.twitch, stream_channel_data["node"], drops_enabled=True))
                else:
                    self.twitch.gui.print(f'Could not load Channel for {stream_channel_data["node"]["game"]["name"]}.\n↳ Stream Title: "{stream_channel_data["node"]["title"]}"')
            return streams
        return []

    async def process_stream_state(self, channel_id: int, message: dict):
        """Process stream state websocket messages."""
        msg_type = message["type"]
        channel = self.channels.get(channel_id)
        if channel is None:
            logger = logging.getLogger("TwitchDrops")
            logger.error(f"Stream state change for a non-existing channel: {channel_id}")
            return
        if msg_type == "viewcount":
            if not channel.online:
                # if it's not online for some reason, set it so
                channel.check_online()
            else:
                viewers = message["viewers"]
                channel.viewers = viewers
                channel.display()
                # logger.debug(f"{channel.name} viewers: {viewers}")
        elif msg_type == "stream-down":
            channel.set_offline()
        elif msg_type == "stream-up":
            channel.check_online()
        elif msg_type == "commercial":
            # skip these
            pass
        else:
            logger = logging.getLogger("TwitchDrops")
            logger.warning(f"Unknown stream state: {msg_type}")

    async def process_stream_update(self, channel_id: int, message: dict):
        """Process stream update websocket messages."""
        # message = {
        #     "channel_id": "12345678",
        #     "type": "broadcast_settings_update",
        #     "channel": "channel._login",
        #     "old_status": "Old title",
        #     "status": "New title",
        #     "old_game": "Old game name",
        #     "game": "New game name",
        #     "old_game_id": 123456,
        #     "game_id": 123456
        # }
        channel = self.channels.get(channel_id)
        if channel is None:
            logger = logging.getLogger("TwitchDrops")
            logger.error(f"Broadcast settings update for a non-existing channel: {channel_id}")
            return
        if message["old_game"] != message["game"]:
            game_change = f", game changed: {message['old_game']} -> {message['game']}"
        else:
            game_change = ''
        logger = logging.getLogger("TwitchDrops")
        logger.log(CALL, f"Channel update from websocket: {channel.name}{game_change}")
        # There's no information about channel tags here, but this event is triggered
        # when the tags change. We can use this to just update the stream data after the change.
        # Use 'set_online' to introduce a delay, allowing for multiple title and tags
        # changes before we update. This eventually calls 'on_channel_update' below.
        channel.check_online()

    def on_channel_update(
        self, channel: Channel, stream_before, stream_after
    ):
        """
        Called by a Channel when it's status is updated (ONLINE, OFFLINE, title/tags change).

        NOTE: 'stream_before' gets dealocated once this function finishes.
        """
        if stream_before is None:
            if stream_after is not None:
                # Channel going ONLINE
                if (
                    self.can_watch(channel)  # we can watch the channel
                    and self.should_switch(channel)  # and we should!
                ):
                    self.twitch.print(_("status", "goes_online").format(channel=channel.name))
                    self.watch(channel)
                else:
                    logger = logging.getLogger("TwitchDrops")
                    logger.info(f"{channel.name} goes ONLINE")
            else:
                # Channel was OFFLINE and stays that way
                logger = logging.getLogger("TwitchDrops")
                logger.log(CALL, f"{channel.name} stays OFFLINE")
        else:
            watching_channel = self.watching_channel.get_with_default(None)
            if (
                watching_channel is not None
                and watching_channel == channel  # the watching channel was the one updated
                and not self.can_watch(channel)   # we can't watch it anymore
            ):
                # NOTE: In these cases, channel was the watching channel
                if stream_after is None:
                    # Channel going OFFLINE
                    self.twitch.print(_("status", "goes_offline").format(channel=channel.name))
                else:
                    # Channel stays ONLINE, but we can't watch it anymore
                    logger = logging.getLogger("TwitchDrops")
                    logger.info(
                        f"{channel.name} status has been updated, switching... "
                        f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                        f"{stream_after.drops_enabled and '✔' or '❌'})"
                    )
                self.twitch.change_state(State.CHANNEL_SWITCH)
            # NOTE: In these cases, it wasn't the watching channel
            elif stream_after is None:
                logger = logging.getLogger("TwitchDrops")
                logger.info(f"{channel.name} goes OFFLINE")
            else:
                # Channel is and stays ONLINE, but has been updated
                logger = logging.getLogger("TwitchDrops")
                logger.info(
                    f"{channel.name} status has been updated "
                    f"(🎁: {stream_before.drops_enabled and '✔' or '❌'} -> "
                    f"{stream_after.drops_enabled and '✔' or '❌'})"
                )
        channel.display()

    def start_watching_task(self):
        """Start the watching task."""
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_loop())

    def stop_watching_task(self):
        """Stop the watching task."""
        if self._watching_task is not None:
            self._watching_task.cancel()
            self._watching_task = None

    def set_drop_update_future(self, future: asyncio.Future[bool] | None):
        """Set the drop update future for websocket communication."""
        self._drop_update = future

    def get_drop_update_future(self) -> asyncio.Future[bool] | None:
        """Get the current drop update future."""
        return self._drop_update
