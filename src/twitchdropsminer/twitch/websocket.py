"""
WebSocket functionality for Twitch Drops Miner.

This module handles all WebSocket-related functionality including:
- WebSocket connections and management
- Drop progress messages
- Points and notifications
- Message processing and routing
"""

from __future__ import annotations

import json
import asyncio
import logging
from time import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Literal, TYPE_CHECKING

import aiohttp

from ..translate import _
from ..exceptions import MinerException, WebsocketClosed
from ..constants import PING_INTERVAL, PING_TIMEOUT, MAX_WEBSOCKETS, WS_TOPICS_LIMIT, CALL, GQL_OPERATIONS
from ..utils import (
    CHARS_ASCII,
    task_wrapper,
    create_nonce,
    json_minify,
    format_traceback,
    AwaitableValue,
    ExponentialBackoff,
)

if TYPE_CHECKING:
    from collections import abc
    from .client import Twitch
    from ..gui import WebsocketStatus
    from ..constants import JsonType, WebsocketTopic


WSMsgType = aiohttp.WSMsgType
logger = logging.getLogger("TwitchDrops")
ws_logger = logging.getLogger("TwitchDrops.websocket")


class WebsocketManager:
    """Manages WebSocket message processing."""

    def __init__(self, twitch: Twitch):
        self.twitch = twitch

    @task_wrapper
    async def process_drops(self, user_id: int, message: dict):
        """Process drops websocket messages."""
        # Message examples:
        # {"type": "drop-progress", data: {"current_progress_min": 3, "required_progress_min": 10}}
        # {"type": "drop-claim", data: {"drop_instance_id": ...}}
        msg_type: str = message["type"]
        if msg_type not in ("drop-progress", "drop-claim"):
            return
        drop_id: str = message["data"]["drop_id"]
        drop = self.twitch.drops.get_drop(drop_id)
        if msg_type == "drop-claim":
            if drop is None:
                logger = logging.getLogger("TwitchDrops")
                logger.error(
                    f"Received a drop claim ID for a non-existing drop: {drop_id}\n"
                    f"Drop claim ID: {message['data']['drop_instance_id']}"
                )
                return
            drop.update_claim(message["data"]["drop_instance_id"])
            campaign = drop.campaign
            mined = await drop.claim()
            drop.display()
            if mined:
                claim_text = (
                    f"{campaign.game.name}\n"
                    f"{drop.rewards_text()} ({campaign.claimed_drops}/{campaign.total_drops})"
                )
                # two different claim texts, becase a new line after the game name
                # looks ugly in the output window - replace it with a space
                self.twitch.print(_("status", "claimed_drop").format(drop=claim_text.replace('\n', ' ')))
                self.twitch.gui.tray.notify(claim_text, _("gui", "tray", "notification_title"))
            else:
                logger = logging.getLogger("TwitchDrops")
                logger.error(f"Drop claim has potentially failed! Drop ID: {drop_id}")
            # About 4-20s after claiming the drop, next drop can be started
            # by re-sending the watch payload. We can test for it by fetching the current drop
            # via GQL, and then comparing drop IDs.
            await asyncio.sleep(4)
            for attempt in range(8):
                context = await self.twitch.gql_request(GQL_OPERATIONS["CurrentDrop"])
                drop_data: JsonType | None = (
                    context["data"]["currentUser"]["dropCurrentSession"]
                )
                if drop_data is None or drop_data["dropID"] != drop.id:
                    break
                await asyncio.sleep(2)
            if campaign.can_earn(self.twitch.streams.watching_channel.get_with_default(None)):
                self.twitch.streams.restart_watching()
            else:
                from constants import State
                self.twitch.change_state(State.INVENTORY_FETCH)
            return
        assert msg_type == "drop-progress"
        if drop is not None:
            drop_text = (
                f"{drop.name} ({drop.campaign.game}, "
                f"{message['data']['current_progress_min']}/"
                f"{message['data']['required_progress_min']})"
            )
        else:
            drop_text = "<Unknown>"
        logger = logging.getLogger("TwitchDrops")
        logger.log(CALL, f"Drop update from websocket: {drop_text}")
        drop_update_future = self.twitch.streams.get_drop_update_future()
        if drop_update_future is None:
            # we aren't actually waiting for a progress update right now, so we can just
            # ignore the event this time
            return
        elif drop is not None and drop.can_earn(self.twitch.streams.watching_channel.get_with_default(None)):
            # the received payload is for the drop we expected
            drop.update_minutes(message["data"]["current_progress_min"])
            drop.display()
            # Let the watch loop know we've handled it here
            drop_update_future.set_result(True)
        else:
            # Sometimes, the drop update we receive doesn't actually match what we're mining.
            # This is a Twitch bug workaround: signal the watch loop to use GQL
            # to get the current drop progress instead.
            drop_update_future.set_result(False)
        self.twitch.streams.set_drop_update_future(None)

    @task_wrapper
    async def process_notifications(self, user_id: int, message: dict):
        """Process notifications websocket messages."""
        if message["type"] == "create-notification":
            data: JsonType = message["data"]["notification"]
            if data["type"] == "user_drop_reward_reminder_notification":
                from constants import State
                self.twitch.change_state(State.INVENTORY_FETCH)
                await self.twitch.gql_request(
                    GQL_OPERATIONS["NotificationsDelete"].with_variables(
                        {"input": {"id": data["id"]}}
                    )
                )

    @task_wrapper
    async def process_points(self, user_id: int, message: dict):
        """Process points websocket messages."""
        # Example payloads:
        # {
        #     "type": "points-earned",
        #     "data": {
        #         "timestamp": "YYYY-MM-DDTHH:MM:SS.UUUUUUUUUZ",
        #         "channel_id": "123456789",
        #         "point_gain": {
        #             "user_id": "12345678",
        #             "channel_id": "123456789",
        #             "total_points": 10,
        #             "baseline_points": 10,
        #             "reason_code": "WATCH",
        #             "multipliers": []
        #         },
        #         "balance": {
        #             "user_id": "12345678",
        #             "channel_id": "123456789",
        #             "balance": 12345
        #         }
        #     }
        # }
        # {
        #     "type": "claim-available",
        #     "data": {
        #         "timestamp":"YYYY-MM-DDTHH:MM:SS.UUUUUUUUUZ",
        #         "claim": {
        #             "id": "4ae6fefd-1234-40ae-ad3d-92254c576a91",
        #             "user_id": "12345678",
        #             "channel_id": "123456789",
        #             "point_gain": {
        #                 "user_id": "12345678",
        #                 "channel_id": "123456789",
        #                 "total_points": 50,
        #                 "baseline_points": 50,
        #                 "reason_code": "CLAIM",
        #                 "multipliers": []
        #             },
        #             "created_at": "YYYY-MM-DDTHH:MM:SSZ"
        #         }
        #     }
        # }
        msg_type = message["type"]
        if msg_type == "points-earned":
            data: JsonType = message["data"]
            channel = self.twitch.streams.channels.get(int(data["channel_id"]))
            points: int = data["point_gain"]["total_points"]
            balance: int = data["balance"]["balance"]
            if channel is not None:
                channel.points = balance
                channel.display()
            self.twitch.print(_("status", "earned_points").format(points=f"{points:3}", balance=balance))
        elif msg_type == "claim-available":
            claim_data = message["data"]["claim"]
            points = claim_data["point_gain"]["total_points"]
            await self.twitch.claim_points(claim_data["channel_id"], claim_data["id"])
            self.twitch.print(_("status", "claimed_points").format(points=points))


class Websocket:
    def __init__(self, pool: WebsocketPool, index: int):
        self._pool: WebsocketPool = pool
        self._twitch: Twitch = pool._twitch
        self._ws_gui: WebsocketStatus = self._twitch.gui.websockets
        self._state_lock = asyncio.Lock()
        # websocket index
        self._idx: int = index
        # current websocket connection
        self._ws: AwaitableValue[aiohttp.ClientWebSocketResponse] = AwaitableValue()
        # set when the websocket needs to be closed or reconnect
        self._closed = asyncio.Event()
        self._reconnect_requested = asyncio.Event()
        # set when the topics changed
        self._topics_changed = asyncio.Event()
        # ping timestamps
        self._next_ping: float = time()
        self._max_pong: float = self._next_ping + PING_TIMEOUT.total_seconds()
        # main task, responsible for receiving messages, sending them, and websocket ping
        self._handle_task: asyncio.Task[None] | None = None
        # topics stuff
        self.topics: dict[str, WebsocketTopic] = {}
        self._submitted: set[WebsocketTopic] = set()
        # notify GUI
        self.set_status("Disconnected")

    @property
    def connected(self) -> bool:
        return self._ws.has_value()

    def wait_until_connected(self):
        return self._ws.wait()

    def set_status(self, status: str | None = None, refresh_topics: bool = False):
        self._twitch.gui.websockets.update(
            self._idx, status=status, topics=(len(self.topics) if refresh_topics else None)
        )

    def request_reconnect(self):
        # reset our ping interval, so we send a PING after reconnect right away
        self._next_ping = time()
        self._reconnect_requested.set()

    async def start(self):
        async with self._state_lock:
            self.start_nowait()
            await self.wait_until_connected()

    def start_nowait(self):
        if self._handle_task is None or self._handle_task.done():
            self._handle_task = asyncio.create_task(self._handle())

    async def stop(self, *, remove: bool = False):
        async with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            ws = self._ws.get_with_default(None)
            if ws is not None:
                self.set_status(_("gui", "websocket", "disconnecting"))
                await ws.close()
            if self._handle_task is not None:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(self._handle_task, timeout=2)
                self._handle_task = None
            if remove:
                self.topics.clear()
                self._topics_changed.set()
                self._twitch.gui.websockets.remove(self._idx)

    def stop_nowait(self, *, remove: bool = False):
        # weird syntax but that's what we get for using a decorator for this
        # return type of 'task_wrapper' is a coro, so we need to instance it for the task
        asyncio.create_task(task_wrapper(self.stop)(remove=remove))

    async def _backoff_connect(
        self, ws_url: str, **kwargs
    ) -> abc.AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
        session = await self._twitch.get_session()
        backoff = ExponentialBackoff(**kwargs)
        if self._twitch.settings.proxy:
            proxy = self._twitch.settings.proxy
        else:
            proxy = None
        for delay in backoff:
            try:
                async with session.ws_connect(ws_url, ssl=True, proxy=proxy) as websocket:
                    yield websocket
                    backoff.reset()
            except (
                asyncio.TimeoutError,
                aiohttp.ClientResponseError,
                aiohttp.ClientConnectionError,
            ):
                ws_logger.info(
                    f"Websocket[{self._idx}] connection problem (sleep: {round(delay)}s)",
                    exc_info=True,
                )
                await asyncio.sleep(delay)
            except RuntimeError:
                ws_logger.warning(
                    f"Websocket[{self._idx}] exiting backoff connect loop "
                    "because session is closed (RuntimeError)"
                )
                break

    @task_wrapper
    async def _handle(self):
        # ensure we're logged in before connecting
        self.set_status(_("gui", "websocket", "initializing"))
        await self._twitch.wait_until_login()
        self.set_status(_("gui", "websocket", "connecting"))
        ws_logger.info(f"Websocket[{self._idx}] connecting...")
        self._closed.clear()
        # Connect/Reconnect loop
        async for websocket in self._backoff_connect(
            "wss://pubsub-edge.twitch.tv/v1", maximum=3*60  # 3 minutes maximum backoff time
        ):
            self._ws.set(websocket)
            self._reconnect_requested.clear()
            # NOTE: _topics_changed doesn't start set,
            # because there's no initial topics we can sub to right away
            self.set_status(_("gui", "websocket", "connected"))
            ws_logger.info(f"Websocket[{self._idx}] connected.")
            try:
                try:
                    while not self._reconnect_requested.is_set():
                        await self._handle_ping()
                        await self._handle_topics()
                        await self._handle_recv()
                finally:
                    self._ws.clear()
                    self._submitted.clear()
                    # set _topics_changed to let the next WS connection resub to the topics
                    self._topics_changed.set()
                # A reconnect was requested
            except WebsocketClosed as exc:
                if exc.received:
                    # server closed the connection, not us - reconnect
                    ws_logger.warning(
                        f"Websocket[{self._idx}] closed unexpectedly: {websocket.close_code}"
                    )
                elif self._closed.is_set():
                    # we closed it - exit
                    ws_logger.info(f"Websocket[{self._idx}] stopped.")
                    self.set_status(_("gui", "websocket", "disconnected"))
                    return
            except Exception:
                ws_logger.exception(f"Exception in Websocket[{self._idx}]")
            self.set_status(_("gui", "websocket", "reconnecting"))
            ws_logger.warning(f"Websocket[{self._idx}] reconnecting...")

    async def _handle_ping(self):
        now = time()
        if now >= self._next_ping:
            self._next_ping = now + PING_INTERVAL.total_seconds()
            self._max_pong = now + PING_TIMEOUT.total_seconds()  # wait for a PONG for up to 10s
            await self.send({"type": "PING"})
        elif now >= self._max_pong:
            # it's been more than 10s and there was no PONG
            ws_logger.warning(f"Websocket[{self._idx}] didn't receive a PONG, reconnecting...")
            self.request_reconnect()

    async def _handle_topics(self):
        if not self._topics_changed.is_set():
            # nothing to do
            return
        self._topics_changed.clear()
        self.set_status(refresh_topics=True)
        auth_state = await self._twitch.get_auth()
        current: set[WebsocketTopic] = set(self.topics.values())
        # handle removed topics
        removed = self._submitted.difference(current)
        if removed:
            topics_list = list(map(str, removed))
            ws_logger.debug(f"Websocket[{self._idx}]: Removing topics: {', '.join(topics_list)}")
            await self.send(
                {
                    "type": "UNLISTEN",
                    "data": {
                        "topics": topics_list,
                        "auth_token": auth_state.access_token,
                    }
                }
            )
            self._submitted.difference_update(removed)
        # handle added topics
        added = current.difference(self._submitted)
        if added:
            topics_list = list(map(str, added))
            ws_logger.debug(f"Websocket[{self._idx}]: Adding topics: {', '.join(topics_list)}")
            await self.send(
                {
                    "type": "LISTEN",
                    "data": {
                        "topics": topics_list,
                        "auth_token": auth_state.access_token,
                    }
                }
            )
            self._submitted.update(added)

    async def _gather_recv(self, messages: list[JsonType], timeout: float = 0.5):
        """
        Gather incoming messages over the timeout specified.
        Note that there's no return value - this modifies `messages` in-place.
        """
        ws = self._ws.get_with_default(None)
        assert ws is not None
        while True:
            raw_message: aiohttp.WSMessage = await ws.receive(timeout=timeout)
            ws_logger.debug(f"Websocket[{self._idx}] received: {raw_message}")
            if raw_message.type is WSMsgType.TEXT:
                message: JsonType = json.loads(raw_message.data)
                messages.append(message)
            elif raw_message.type is WSMsgType.CLOSE:
                raise WebsocketClosed(received=True)
            elif raw_message.type is WSMsgType.CLOSED:
                raise WebsocketClosed(received=False)
            elif raw_message.type is WSMsgType.CLOSING:
                pass  # skip these
            elif raw_message.type is WSMsgType.ERROR:
                ws_logger.error(
                    f"Websocket[{self._idx}] error: {format_traceback(raw_message.data)}"
                )
                raise WebsocketClosed()
            else:
                ws_logger.error(f"Websocket[{self._idx}] error: Unknown message: {raw_message}")

    def _handle_message(self, message):
        # request the assigned topic to process the response
        topic = self.topics.get(message["data"]["topic"])
        if topic is not None:
            # use a task to not block the websocket
            asyncio.create_task(topic(json.loads(message["data"]["message"])))

    async def _handle_recv(self):
        """
        Handle receiving messages from the websocket.
        """
        # listen over 0.5s for incoming messages
        messages: list[JsonType] = []
        with suppress(asyncio.TimeoutError):
            await self._gather_recv(messages, timeout=0.5)
        # process them
        for message in messages:
            msg_type = message["type"]
            if msg_type == "MESSAGE":
                self._handle_message(message)
            elif msg_type == "PONG":
                # move the timestamp to something much later
                self._max_pong = self._next_ping
            elif msg_type == "RESPONSE":
                # no special handling for these (for now)
                pass
            elif msg_type == "RECONNECT":
                # We've received a reconnect request
                ws_logger.warning(f"Websocket[{self._idx}] requested reconnect.")
                self.request_reconnect()
            else:
                ws_logger.warning(f"Websocket[{self._idx}] received unknown payload: {message}")

    def add_topics(self, topics_set: set[WebsocketTopic]):
        changed: bool = False
        while topics_set and len(self.topics) < WS_TOPICS_LIMIT:
            topic = topics_set.pop()
            self.topics[str(topic)] = topic
            changed = True
        if changed:
            self._topics_changed.set()

    def remove_topics(self, topics_set: set[str]):
        existing = topics_set.intersection(self.topics.keys())
        if not existing:
            # nothing to remove from here
            return
        topics_set.difference_update(existing)
        for topic in existing:
            del self.topics[topic]
        self._topics_changed.set()

    async def send(self, message: JsonType):
        ws = self._ws.get_with_default(None)
        assert ws is not None
        if message["type"] != "PING":
            message["nonce"] = create_nonce(CHARS_ASCII, 30)
        await ws.send_json(message, dumps=json_minify)
        ws_logger.debug(f"Websocket[{self._idx}] sent: {message}")


class WebsocketPool:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._running = asyncio.Event()
        self.websockets: list[Websocket] = []

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def wait_until_connected(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._running.wait()

    async def start(self):
        self._running.set()
        await asyncio.gather(*(ws.start() for ws in self.websockets))

    async def stop(self, *, clear_topics: bool = False):
        self._running.clear()
        await asyncio.gather(*(ws.stop(remove=clear_topics) for ws in self.websockets))

    def add_topics(self, topics: abc.Iterable[WebsocketTopic]):
        # ensure no topics end up duplicated
        topics_set = set(topics)
        if not topics_set:
            # nothing to add
            return
        topics_set.difference_update(*(ws.topics.values() for ws in self.websockets))
        if not topics_set:
            # none left to add
            return
        for ws_idx in range(MAX_WEBSOCKETS):
            if ws_idx < len(self.websockets):
                # just read it back
                ws = self.websockets[ws_idx]
            else:
                # create new
                ws = Websocket(self, ws_idx)
                if self.running:
                    ws.start_nowait()
                self.websockets.append(ws)
            # ask websocket to take any topics it can - this modifies the set in-place
            ws.add_topics(topics_set)
            # see if there's any leftover topics for the next websocket connection
            if not topics_set:
                return
        # if we're here, there were leftover topics after filling up all websockets
        raise MinerException("Maximum topics limit has been reached")

    def remove_topics(self, topics: abc.Iterable[str]):
        topics_set = set(topics)
        if not topics_set:
            # nothing to remove
            return
        for ws in self.websockets:
            ws.remove_topics(topics_set)
        # count up all the topics - if we happen to have more websockets connected than needed,
        # stop the last one and recycle topics from it - repeat until we have enough
        recycled_topics: list[WebsocketTopic] = []
        while True:
            count = sum(len(ws.topics) for ws in self.websockets)
            if count <= (len(self.websockets) - 1) * WS_TOPICS_LIMIT:
                ws = self.websockets.pop()
                recycled_topics.extend(ws.topics.values())
                ws.stop_nowait(remove=True)
            else:
                break
        if recycled_topics:
            self.add_topics(recycled_topics)
