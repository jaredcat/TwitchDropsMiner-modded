"""
Main Twitch client class for Twitch Drops Miner.

This module contains the main Twitch client class that orchestrates
all Twitch-related functionality.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta
from time import time
from typing import Any, Final, NoReturn, TYPE_CHECKING, cast, overload
from contextlib import suppress, asynccontextmanager
from itertools import chain

import aiohttp
from yarl import URL

from ..cache import CurrentSeconds
from ..translate import _
from ..gui import GUIManager
from ..channel import Channel
from .websocket import WebsocketPool
from ..inventory import DropsCampaign
from ..exceptions import (
    MinerException,
    CaptchaRequired,
    ExitRequest,
    LoginException,
    ReloadRequest,
    RequestInvalid,
)
from ..utils import (
    task_wrapper,
    OrderedSet,
    AwaitableValue,
    chunk,
    first_to_complete,
    ExponentialBackoff,
)
from ..constants import (
    CALL,
    MAX_CHANNELS,
    WATCH_INTERVAL,
    State,
    ClientType,
    WebsocketTopic,
    PRIORITY_ALGORITHM_LIST,
    PRIORITY_ALGORITHM_ADAPTIVE,
    PRIORITY_ALGORITHM_BALANCED,
    PRIORITY_ALGORITHM_ENDING_SOONEST,
    COOKIES_PATH,
    GQL_OPERATIONS,
)

if TYPE_CHECKING:
    from ..utils import Game
    from ..gui import LoginForm
    from ..inventory import TimedDrop
    from ..constants import JsonType
    from .auth import _AuthState
    from .drops import DropsManager
    from .streams import StreamsManager
    from .websocket import WebsocketManager


class Twitch:
    """Main Twitch client class."""

    def __init__(self, settings):
        self.settings = settings
        # State management
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self.wanted_games: dict[Game, int] = {}
        # Client type, session and auth
        self._client_type: ClientType = ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        from .auth import _AuthState
        self._auth_state: _AuthState = _AuthState(self)
        # GUI
        self.gui = GUIManager(self)
        # Websocket
        self.websocket = WebsocketPool(self)
        # Maintenance task
        self._mnt_task: asyncio.Task[None] | None = None

        # Initialize specialized managers
        from .drops import DropsManager
        from .streams import StreamsManager
        from .websocket import WebsocketManager
        self.drops = DropsManager(self)
        self.streams = StreamsManager(self)
        self.websocket_mgr = WebsocketManager(self)

        # Backward compatibility properties
        self.inventory = self.drops.inventory
        self.channels = self.streams.channels
        self.watching_channel = self.streams.watching_channel

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers=self._auth_state.headers(),
            )
        return self._session

    async def shutdown(self) -> None:
        """Shutdown the client and cleanup resources."""
        if self._session is not None:
            await self._session.close()
        if self.websocket is not None:
            await self.websocket.shutdown()
        if self._mnt_task is not None:
            self._mnt_task.cancel()
        if self._watching_task is not None:
            self._watching_task.cancel()

    def wait_until_login(self) -> asyncio.Coroutine[Any, Any, bool]:
        """Wait until login is complete."""
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        """Change the current state."""
        self._state = state
        self._state_change.set()
        self._state_change.clear()

    def state_change(self, state: State) -> callable:
        """Return a callable that changes state when called."""
        from functools import partial
        return partial(self.change_state, state)

    def close(self):
        """Request the client to close."""
        self.gui.close()

    def prevent_close(self):
        """Prevent the client from closing."""
        self.gui.prevent_close()

    def print(self, message: str):
        """Print a message to the GUI."""
        self.gui.print(message)

    def save(self, *, force: bool = False) -> None:
        """Save application state."""
        self.gui.save(force=force)

    def get_priority(self, channel: Channel) -> int:
        """Get priority for a channel based on current algorithm."""
        algorithm = self.settings.priority_algorithm

        if algorithm == PRIORITY_ALGORITHM_LIST:
            return self._get_list_priority(channel)
        elif algorithm == PRIORITY_ALGORITHM_ADAPTIVE:
            return self._get_adaptive_priority(channel)
        elif algorithm == PRIORITY_ALGORITHM_BALANCED:
            return self._get_balanced_priority(channel)
        elif algorithm == PRIORITY_ALGORITHM_ENDING_SOONEST:
            return self._get_ending_soonest_priority(channel)
        else:
            # Default to list priority
            return self._get_list_priority(channel)

    def _get_list_priority(self, channel: Channel) -> int:
        """Get priority based on user-defined list."""
        if not hasattr(channel, 'game') or not channel.game:
            return 0
        return self.wanted_games.get(channel.game, 0)

    def _get_adaptive_priority(self, channel: Channel) -> int:
        """Get adaptive priority based on multiple factors."""
        # Simplified implementation
        base_priority = self._get_list_priority(channel)
        if hasattr(channel, 'viewers') and channel.viewers:
            # Boost priority for channels with more viewers
            base_priority += min(channel.viewers // 1000, 10)
        return base_priority

    def _get_balanced_priority(self, channel: Channel) -> int:
        """Get balanced priority considering multiple factors."""
        # Simplified implementation
        return self._get_list_priority(channel)

    def _get_ending_soonest_priority(self, channel: Channel) -> int:
        """Get priority based on campaigns ending soonest."""
        # Simplified implementation
        return self._get_list_priority(channel)

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        """Key function for sorting by viewers."""
        return channel.viewers or 0

    def _calculate_weighted_priority(self, campaign, user_priority: int) -> float:
        """Calculate weighted priority for a campaign."""
        # Simplified implementation
        return float(user_priority)

    def _calculate_smart_priority(self, campaign, user_priority: int) -> float:
        """Calculate smart priority for a campaign."""
        # Simplified implementation
        return float(user_priority)

    async def run(self):
        """Main run method."""
        while True:
            try:
                await self._run()
                break
            except ReloadRequest:
                await self.shutdown()
            except ExitRequest:
                break
            except aiohttp.ContentTypeError as exc:
                raise MinerException(_("login", "unexpected_content")) from exc

    async def _run(self):
        """
        Main method that runs the whole client.

        Here, we manage several things, specifically:
        • Fetching the drops inventory to make sure that everything we can claim, is claimed
        • Selecting a stream to watch, and watching it
        • Changing the stream that's being watched if necessary
        """
        self.gui.start()
        auth_state = await self.get_auth()
        await self.websocket.start()
        # NOTE: watch task is explicitly restarted on each new run
        if self._watching_task is not None:
            self._watching_task.cancel()
        self._watching_task = asyncio.create_task(self._watch_loop())
        # Add default topics
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic("User", "CommunityPoints", auth_state.user_id, self.process_points),
            WebsocketTopic(
                "User", "Notifications", auth_state.user_id, self.process_notifications
            ),
        ])
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        while True:
            if self._state is State.IDLE:
                self.gui.status.update(_("gui", "status", "idle"))
                self.stop_watching()
                # clear the flag and wait until it's set again
                self._state_change.clear()
            elif self._state is State.INVENTORY_FETCH:
                # ensure the websocket is running
                await self.websocket.start()
                await self.fetch_inventory()
                self.gui.set_games(set(campaign.game for campaign in self.inventory))
                # Save state on every inventory fetch
                self.save()
                self.change_state(State.GAMES_UPDATE)
            elif self._state is State.GAMES_UPDATE:
                # claim drops from expired and active campaigns
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                await drop.claim()
                # figure out which games we want
                self.wanted_games.clear()
                priorities = self.gui.settings.priorities()
                priority_algorithm = self.settings.priority_algorithm
                campaigns = self.inventory
                filtered_campaigns = list(filter(self.filter_campaigns, campaigns))

                # Sort campaigns based on selected algorithm
                if priority_algorithm == PRIORITY_ALGORITHM_ENDING_SOONEST:
                    # Sort by end_at time (ending soonest first)
                    filtered_campaigns.sort(key=lambda c: c.ends_at)
                    for i, campaign in enumerate(filtered_campaigns):
                        game = campaign.game
                        game_priority = priorities.get(game.name, 0)
                        if game_priority:
                            score = len(filtered_campaigns) - i
                            self.wanted_games[game] = score
                        else:
                            self.wanted_games[game] = -i
                elif priority_algorithm == PRIORITY_ALGORITHM_BALANCED:
                    # Weighted priority: blend user priority with time urgency
                    for i, campaign in enumerate(filtered_campaigns):
                        game = campaign.game
                        game_priority = priorities.get(game.name, 0)
                        if game_priority:
                            # Calculate balanced score
                            balanced_score = self._calculate_weighted_priority(campaign, game_priority)
                            self.wanted_games[game] = balanced_score
                        else:
                            # Non-priority games: use time-based sorting
                            time_remaining = (campaign.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600
                            self.wanted_games[game] = -time_remaining
                elif priority_algorithm == PRIORITY_ALGORITHM_ADAPTIVE:
                    # Smart priority: ensure higher priority games complete before lower ones
                    for i, campaign in enumerate(filtered_campaigns):
                        game = campaign.game
                        game_priority = priorities.get(game.name, 0)
                        if game_priority:
                            adaptive_score = self._calculate_smart_priority(campaign, game_priority)
                            self.wanted_games[game] = adaptive_score
                        else:
                            # Non-priority games: use time-based sorting
                            time_remaining = (campaign.ends_at - datetime.now(timezone.utc)).total_seconds() / 3600
                            self.wanted_games[game] = -time_remaining
                else:
                    # Default priority list: use user-defined order (original behavior)
                    for i, campaign in enumerate(filtered_campaigns):
                        game = campaign.game
                        game_priority = priorities.get(game.name, 0)
                        if game_priority:
                            self.wanted_games[game] = game_priority
                        else:
                            self.wanted_games[game] = -i

                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)
            elif self._state is State.CHANNELS_CLEANUP:
                self.gui.status.update(_("gui", "status", "cleanup"))
                if not self.wanted_games or full_cleanup:
                    # no games selected or we're doing full cleanup: remove everything
                    to_remove_channels: list[Channel] = list(channels.values())
                else:
                    # remove all channels that:
                    to_remove_channels = [
                        channel
                        for channel in channels.values()
                        if (
                            not channel.acl_based  # aren't ACL-based
                            and (
                                channel.offline  # and are offline
                                # or online but aren't streaming the game we want anymore
                                or (channel.game is None or channel.game not in self.wanted_games)
                            )
                        )
                    ]
                full_cleanup = False
                if to_remove_channels:
                    to_remove_topics: list[str] = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    for channel in to_remove_channels:
                        del channels[channel.id]
                        channel.remove()
                    del to_remove_channels, to_remove_topics
                if self.wanted_games:
                    self.change_state(State.CHANNELS_FETCH)
                else:
                    # with no games available, we switch to IDLE after cleanup
                    self.print(_("status", "no_campaign"))
                    with open('healthcheck.timestamp', 'w') as f:
                        f.write(str(int(time())))
                    self.change_state(State.IDLE)
            elif self._state is State.CHANNELS_FETCH:
                self.gui.status.update(_("gui", "status", "gathering"))
                # start with all current channels, clear the memory and GUI
                new_channels: OrderedSet[Channel] = OrderedSet(channels.values())
                channels.clear()
                self.gui.channels.clear()
                # gather and add ACL channels from campaigns
                # NOTE: we consider only campaigns that can be progressed
                # NOTE: we use another set so that we can set them online separately
                no_acl: set[Game] = set()
                acl_channels: OrderedSet[Channel] = OrderedSet()
                next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
                for campaign in self.inventory:
                    if (
                        campaign.game in self.wanted_games
                        and campaign.can_earn_within(next_hour)
                    ):
                        if campaign.allowed_channels:
                            acl_channels.update(campaign.allowed_channels)
                        else:
                            no_acl.add(campaign.game)
                # remove all ACL channels that already exist from the other set
                acl_channels.difference_update(new_channels)
                # use the other set to set them online if possible
                # if acl_channels:
                #     await asyncio.gather(*(
                #         channel.update_stream(trigger_events=False)
                #         for channel in acl_channels
                #     ))
                # finally, add them as new channels
                new_channels.update(acl_channels)
                for game in no_acl:
                    # for every campaign without an ACL, for it's game,
                    # add a list of live channels with drops enabled
                    new_channels.update(await self.get_live_streams(game))
                # sort them descending by viewers, by priority and by game priority
                # NOTE: We can drop OrderedSet now because there's no more channels being added
                ordered_channels: list[Channel] = sorted(
                    new_channels, key=self._viewers_key, reverse=True
                )
                ordered_channels.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered_channels.sort(key=self.get_priority, reverse=True)
                # ensure that we won't end up with more channels than we can handle
                # NOTE: we trim from the end because that's where the non-priority,
                # offline (or online but low viewers) channels end up
                to_remove_channels = ordered_channels[MAX_CHANNELS:]
                ordered_channels = ordered_channels[:MAX_CHANNELS]
                if to_remove_channels:
                    # tracked channels and gui were cleared earlier, so no need to do it here
                    # just make sure to unsubscribe from their topics
                    to_remove_topics = []
                    for channel in to_remove_channels:
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamState", channel.id)
                        )
                        to_remove_topics.append(
                            WebsocketTopic.as_str("Channel", "StreamUpdate", channel.id)
                        )
                    self.websocket.remove_topics(to_remove_topics)
                    del to_remove_channels, to_remove_topics
                # set our new channel list
                for channel in ordered_channels:
                    channels[channel.id] = channel
                    channel.display(add=True)
                # subscribe to these channel's state updates
                to_add_topics: list[WebsocketTopic] = []
                for channel_id in channels:
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamState", channel_id, self.process_stream_state
                        )
                    )
                    to_add_topics.append(
                        WebsocketTopic(
                            "Channel", "StreamUpdate", channel_id, self.process_stream_update
                        )
                    )
                self.websocket.add_topics(to_add_topics)
                # relink watching channel after cleanup,
                # or stop watching it if it no longer qualifies
                # NOTE: this replaces 'self.watching_channel's internal value with the new object
                watching_channel = self.watching_channel.get_with_default(None)
                if watching_channel is not None:
                    new_watching: Channel | None = channels.get(watching_channel.id)
                    if new_watching is not None and self.can_watch(new_watching):
                        self.watch(new_watching, update_status=False)
                    else:
                        # we've removed a channel we were watching
                        self.stop_watching()
                    del new_watching
                # pre-display the active drop with a substracted minute
                for channel in channels.values():
                    # check if there's any channels we can watch first
                    if self.can_watch(channel):
                        if (active_drop := self.get_active_drop(channel)) is not None:
                            active_drop.display(countdown=False, subone=True)
                        del active_drop
                        break
                self.change_state(State.CHANNEL_SWITCH)
                del (
                    no_acl,
                    acl_channels,
                    new_channels,
                    to_add_topics,
                    ordered_channels,
                    watching_channel,
                )
            elif self._state is State.CHANNEL_SWITCH:
                self.gui.status.update(_("gui", "status", "switching"))
                # Change into the selected channel, stay in the watching channel,
                # or select a new channel that meets the required conditions
                new_watching = None
                selected_channel = self.gui.channels.get_selection()
                if selected_channel is not None and self.can_watch(selected_channel):
                    # selected channel is checked first, and set as long as we can watch it
                    new_watching = selected_channel
                else:
                    # other channels additionally need to have a good reason
                    # for a switch (including the watching one)
                    # NOTE: we need to sort the channels every time because one channel
                    # can end up streaming any game - channels aren't game-tied
                    for channel in sorted(channels.values(), key=self.get_priority, reverse=True):
                        if self.can_watch(channel) and self.should_switch(channel):
                            new_watching = channel
                            break
                watching_channel = self.watching_channel.get_with_default(None)
                if new_watching is not None:
                    # if we have a better switch target - do so
                    self.watch(new_watching)
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                elif watching_channel is not None:
                    # otherwise, continue watching what we had before
                    self.gui.status.update(
                        _("status", "watching").format(channel=watching_channel.name)
                    )
                    # break the state change chain by clearing the flag
                    self._state_change.clear()
                else:
                    # not watching anything and there isn't anything to watch either
                    self.print(_("status", "no_channel"))
                    with open('healthcheck.timestamp', 'w') as f:
                        f.write(str(int(time())))
                    self.change_state(State.IDLE)
                del new_watching, selected_channel, watching_channel
            elif self._state is State.EXIT:
                self.gui.status.update(_("gui", "status", "exiting"))
                # we've been requested to exit the application
                break
            await self._state_change.wait()

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        if (viewers := channel.viewers) is not None:
            return viewers
        return -1

    async def get_auth(self) -> _AuthState:
        """Get authentication state."""
        await self._auth_state.validate()
        return self._auth_state

    # Placeholder methods for websocket message processing
    @task_wrapper
    async def process_drops(self, user_id: int, message: dict):
        """Process drops websocket messages."""
        pass

    @task_wrapper
    async def process_notifications(self, user_id: int, message: dict):
        """Process notifications websocket messages."""
        pass

    @task_wrapper
    async def process_points(self, user_id: int, message: dict):
        """Process points websocket messages."""
        pass

    async def _watch_loop(self) -> NoReturn:
        """Main watching loop."""
        interval: float = WATCH_INTERVAL.total_seconds()
        while True:
            channel: Channel = await self.watching_channel.get()
            succeeded, repeat_now = await channel.send_watch()
            logger = logging.getLogger("TwitchDrops")
            logger.log(CALL,f"returned watch, succeeded: {succeeded}, repeat_new: {repeat_now}")
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
                    context = await self.gql_request(GQL_OPERATIONS["CurrentDrop"])
                    drop_data: JsonType | None = (
                        context["data"]["currentUser"]["dropCurrentSession"]
                    )
                    if drop_data is not None:
                        drop = self._drops.get(drop_data["dropID"])
                        if drop is None:
                            use_active = True
                            # usually this means there was a campaign changed between reloads
                            logger.info("Missing drop detected, reloading...")
                            self.change_state(State.INVENTORY_FETCH)
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
                    if (drop := self.get_active_drop()) is not None:
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

    async def _watch_sleep(self, delay: float) -> None:
        # we use wait_for here to allow an asyncio.sleep-like that can be ended prematurely
        self._watching_restart.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)

    @task_wrapper
    async def _maintenance_task(self) -> None:
        claim_period = timedelta(minutes=30)
        max_period = timedelta(hours=1)
        now = datetime.now(timezone.utc)
        next_period = now + max_period
        while True:
            # exit if there's no need to repeat the loop
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            next_trigger = min(now + claim_period, next_period)
            trigger_cleanup = False
            while self._mnt_triggers and (switch_trigger := self._mnt_triggers[0]) <= next_trigger:
                trigger_cleanup = True
                self._mnt_triggers.popleft()
                next_trigger = switch_trigger
            if next_trigger == next_period:
                trigger_type: str = "Reload"
            elif trigger_cleanup:
                trigger_type = "Cleanup"
            else:
                trigger_type = "Points"
            logger = logging.getLogger("TwitchDrops")
            logger.log(
                CALL,
                (
                    "Maintenance task waiting until: "
                    f"{next_trigger.astimezone().strftime('%X')} ({trigger_type})"
                )
            )
            await asyncio.sleep((next_trigger - now).total_seconds())
            # exit after waiting, before the actions
            now = datetime.now(timezone.utc)
            if now >= next_period:
                break
            if trigger_cleanup:
                logger.log(CALL, "Maintenance task requests channels cleanup")
                self.change_state(State.CHANNELS_CLEANUP)
            # ensure that we don't have unclaimed points bonus
            watching_channel = self.watching_channel.get_with_default(None)
            if watching_channel is not None:
                try:
                    await watching_channel.claim_bonus()
                except Exception:
                    pass  # we intentionally silently skip anything else
        # this triggers this task restart every (up to) 60 minutes
        logger.log(CALL, "Maintenance task requests a reload")
        self.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        """
        Determines if the given channel qualifies as a watching candidate.
        """
        if not self.wanted_games:
            return False
        # exit early if
        if (
            not channel.online  # stream is offline
            # or not channel.drops_enabled  # drops aren't enabled
            # there's no game or it's not one of the games we've selected
            or (game := channel.game) is None or game not in self.wanted_games
        ):
            return False
        # check if we can progress any campaign for the played game
        for campaign in self.inventory:
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
        channel_order = self.get_priority(channel)
        watching_order = self.get_priority(watching_channel)
        return (
            # this channel's game is higher order than the watching one's
            channel_order > watching_order
            or channel_order == watching_order  # or the order is the same
            # and this channel is ACL-based and the watching channel isn't
            and channel.acl_based > watching_channel.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self.gui.channels.set_watching(channel)
        self.watching_channel.set(channel)
        if update_status:
            status_text = _("status", "watching").format(channel=channel.name)
            self.print(status_text)
            self.gui.status.update(status_text)

    def stop_watching(self):
        self.gui.clear_drop()
        self.watching_channel.clear()
        self.gui.channels.clear_watching()

    def restart_watching(self):
        self.gui.progress.stop_timer()
        self._watching_restart.set()

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: dict):
        """Process stream state websocket messages - delegates to streams manager."""
        await self.streams.process_stream_state(channel_id, message)

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: dict):
        """Process stream update websocket messages - delegates to streams manager."""
        await self.streams.process_stream_update(channel_id, message)

    def on_channel_update(
        self, channel: Channel, stream_before, stream_after
    ):
        """Handle channel updates - delegates to streams manager."""
        self.streams.on_channel_update(channel, stream_before, stream_after)

    @task_wrapper
    async def process_drops(self, user_id: int, message: dict):
        """Process drops websocket messages - delegates to websocket manager."""
        await self.websocket_mgr.process_drops(user_id, message)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: dict):
        """Process notifications websocket messages - delegates to websocket manager."""
        await self.websocket_mgr.process_notifications(user_id, message)

    @task_wrapper
    async def process_points(self, user_id: int, message: dict):
        """Process points websocket messages - delegates to websocket manager."""
        await self.websocket_mgr.process_points(user_id, message)

    @asynccontextmanager
    async def request(
        self, method: str, url: URL | str, *, invalidate_after: datetime | None = None, return_error: bool = False, **kwargs
    ):
        session = await self.get_session()
        method = method.upper()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        logger = logging.getLogger("TwitchDrops")
        logger.debug(f"Request: ({method=}, {url=}, {kwargs=})")
        session_timeout = timedelta(seconds=session.timeout.total or 0)
        backoff = ExponentialBackoff(maximum=3*60)
        for delay in backoff:
            if self.gui.close_requested:
                raise ExitRequest()
            elif (
                invalidate_after is not None
                # account for the expiration landing during the request
                and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
            ):
                raise RequestInvalid()
            try:
                response: aiohttp.ClientResponse | None = None
                response = await self.gui.coro_unless_closed(
                    session.request(method, url, **kwargs)
                )
                assert response is not None
                logger.debug(f"Response: {response.status}: {response}")
                if response.status < 500 or return_error:
                    # pre-read the response to avoid getting errors outside of the context manager
                    raw_response = await response.read()  # noqa
                    yield response
                    return
                self.print(_("error", "site_down").format(seconds=round(delay)) + f"\nResponse: {response}" + f"\nStatus: {response.status}")
            except aiohttp.ClientConnectorCertificateError:  # type: ignore[unused-ignore]
                # for a case where SSL verification fails
                raise
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                # just so that quick retries that often happen, aren't shown
                if backoff.steps > 1:
                    self.print(_("error", "no_connection").format(seconds=round(delay)))
            finally:
                if response is not None:
                    response.release()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.gui.wait_until_closed(), timeout=delay)

    @overload
    async def gql_request(self, ops) -> JsonType:
        ...

    @overload
    async def gql_request(self, ops: list) -> list[JsonType]:
        ...

    async def gql_request(
        self, ops
    ) -> JsonType | list[JsonType]:
        gql_logger = logging.getLogger("TwitchDrops.gql")
        gql_logger.debug(f"GQL Request: {ops}")
        backoff = ExponentialBackoff(maximum=60)
        for delay in backoff:
            try:
                auth_state = await self.get_auth()
                async with self.request(
                    "POST",
                    "https://gql.twitch.tv/gql",
                    json=ops,
                    headers=auth_state.headers(user_agent=self._client_type.USER_AGENT, gql=True),
                    invalidate_after=getattr(auth_state, "integrity_expires", None),
                ) as response:
                    response_json: JsonType | list[JsonType] = await response.json()
            except RequestInvalid:
                continue
            gql_logger.debug(f"GQL Response: {response_json}")
            orig_response = response_json
            if isinstance(response_json, list):
                response_list = response_json
            else:
                response_list = [response_json]
            force_retry: bool = False
            for response_json in response_list:
                if "errors" in response_json:
                    additional_message = ""
                    for error_dict in response_json["errors"]:
                        if "message" in error_dict:
                            if error_dict["message"] in (
                                # "service error",
                                "service unavailable",
                                "service timeout",
                                "context deadline exceeded",
                            ):
                                force_retry = True
                                break
                            elif error_dict["message"] in "PersistedQueryNotFound":
                                additional_message = (
                                "\n\nPersistedQueryNotFound can often dissapear by itself. You can also contribute by:\n"
                                "1. Opening Twitch with your Browser\n"
                                "2. Opening Developer Toools (f12)\n"
                                "3. Going to Network\n"
                                "4. Reloading and navigating around Twitch (streams, drop page, game search etc.)\n"
                                "    - Claiming a Drop and ChannelPoints is required for those 2 queries\n"
                                "5. Filter URLs by \"gql\""
                                "6. Searching REQUEST CONTENTS (search icon, not filtering url) for all the things under \"GQL_OPERATIONS\" in constants.py\n"
                                "    - The second string, so \"VideoPlayerStreamInfoOverlayChannel\", not \"GetStreamInfo\"\n"
                                "7. Opening the requests under gql.twitch.tv"
                                "8. Going to Request (not response, you might have to disable raw or otherwise set it to json view)"
                                "9. One should have a \"sha256Hash\" under [number]->extensions->persistedQuery"
                                "    - MAKE SURE the \"operationName\" under [number] is actually the one you want!!!"
                                "10. Replacing the hash in constants.py and making a pull request on GitHub\n"
                                "    - Please document all the queries you checked, even if they didn't change, or just check all\n"
                                "\nIf this is unclear, tell me on GitHub and I'll try to give better instruictions\n"
                                "Issue #126 is tracking a possible permanent solution, if you can help with that\n"
                                "\nThanks ;)"
                                )
                    else:
                        raise MinerException(f"GQL error: {response_json['errors']}{additional_message if additional_message else ''}")
                if force_retry:
                    break
            else:
                return orig_response
            await asyncio.sleep(delay)
        raise MinerException()

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        merged = {}
        for key in set(chain(primary_data.keys(), secondary_data.keys())):
            in_primary = key in primary_data
            if in_primary and key in secondary_data:
                vp = primary_data[key]
                vs = secondary_data[key]
                if not isinstance(vp, type(vs)) or not isinstance(vs, type(vp)):
                    raise MinerException("Inconsistent merge data")
                if isinstance(vp, dict):  # both are dicts
                    merged[key] = self._merge_data(vp, vs)
                else:
                    # use primary value
                    merged[key] = vp
            elif in_primary:
                merged[key] = primary_data[key]
            else:  # in campaigns only
                merged[key] = secondary_data[key]
        return merged

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.get_auth()
        response_list: list[JsonType] = await self.gql_request(
            [
                GQL_OPERATIONS["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {
            (campaign_data := response_json["data"]["user"]["dropCampaign"])["id"]: campaign_data
            for response_json in response_list
        }
        return self._merge_data(campaign_ids, fetched_data)

    def filter_campaigns(self, campaign: DropsCampaign):
        exclude = self.settings.exclude
        priority = self.settings.priority
        priority_only = self.settings.priority_only
        unlinked_campaigns = self.settings.unlinked_campaigns
        game = campaign.game
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        if (
            game not in self.wanted_games # isn't already there
            and game.name not in exclude # and isn't excluded
            # and isn't excluded by priority_only
            and (not priority_only or game.name in priority)
            # and user wants unlinked games or the game is linked
            and (unlinked_campaigns or campaign.linked)
            # and can be progressed within the next hour
            and campaign.can_earn_within(next_hour)
        ):
            return True
        return False

    async def fetch_inventory(self) -> None:
        """Fetch drops inventory - delegates to drops manager."""
        await self.drops.fetch_inventory()

    def get_active_drop(self, channel: Channel | None = None) -> TimedDrop | None:
        """Get active drop - delegates to drops manager."""
        return self.drops.get_active_drop(channel)

    async def get_live_streams(self, game: Game, *, limit: int = 30) -> list[Channel]:
        """Get live streams - delegates to streams manager."""
        return await self.streams.get_live_streams(game, limit=limit)

    async def claim_points(self, channel_id: str | int, claim_id: str) -> None:
        await self.gql_request(
            GQL_OPERATIONS["ClaimCommunityPoints"].with_variables(
                {"input": {"channelID": str(channel_id), "claimID": claim_id}}
            )
        )
