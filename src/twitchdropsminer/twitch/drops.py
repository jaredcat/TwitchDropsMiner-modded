"""
Drops and campaign management for Twitch Drops Miner.

This module handles all drop-related functionality including:
- Campaign fetching and processing
- Drop progress tracking
- Inventory management
- Priority algorithms
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from itertools import chain
from typing import TYPE_CHECKING, cast

from ..cache import CurrentSeconds
from ..translate import _
from ..inventory import DropsCampaign
from ..exceptions import ExitRequest, MinerException
from ..utils import chunk, timestamp
from ..constants import (
    GQL_OPERATIONS,
    PRIORITY_ALGORITHM_ENDING_SOONEST,
    PRIORITY_ALGORITHM_ADAPTIVE,
    PRIORITY_ALGORITHM_BALANCED,
)

if TYPE_CHECKING:
    from .client import Twitch
    from .auth import _AuthState
    from ..constants import JsonType
    from ..utils import Game
    from ..inventory import TimedDrop


class DropsManager:
    """Manages drops and campaign functionality."""

    def __init__(self, twitch: Twitch):
        self.twitch = twitch
        self._drops: dict[str, TimedDrop] = {}
        self.inventory: list[DropsCampaign] = []
        self._mnt_triggers: list[datetime] = []

    def _merge_data(self, primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        """Merge two data dictionaries, preferring primary values."""
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
        """Fetch detailed campaign data for a chunk of campaigns."""
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self.twitch.get_auth()
        response_list: list[JsonType] = await self.twitch.gql_request(
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

    def filter_campaigns(self, campaign: DropsCampaign) -> bool:
        """Filter campaigns based on user settings."""
        exclude = self.twitch.settings.exclude
        priority = self.twitch.settings.priority
        priority_only = self.twitch.settings.priority_only
        unlinked_campaigns = self.twitch.settings.unlinked_campaigns
        game = campaign.game
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

        if (
            game not in self.twitch.wanted_games  # isn't already there
            and game.name not in exclude  # and isn't excluded
            # and isn't excluded by priority_only
            and (not priority_only or game.name in priority)
            # and user wants unlinked games or the game is linked
            and (unlinked_campaigns or campaign.linked)
            # and can be progressed within the next hour
            and campaign.can_earn_within(next_hour)
        ):
            return True
        return False

    def _calculate_weighted_priority(self, campaign: DropsCampaign, user_priority: int) -> float:
        """
        Calculate weighted priority score: Balanced blend of user priority and time urgency.

        This algorithm sits in the middle of the spectrum between pure priority and pure time.
        It always considers both factors, making it more time-sensitive than Smart but more
        priority-respectful than Ending Soonest.

        Algorithm: 60% user priority + 40% time urgency

        Args:
            campaign: The campaign to calculate priority for
            user_priority: User-defined priority value (higher = more important)

        Returns:
            Weighted priority score (higher = more important)
        """
        current_time = datetime.now(timezone.utc)
        time_remaining_hours = (campaign.ends_at - current_time).total_seconds() / 3600

        if time_remaining_hours <= 0:
            return -float('inf')  # Expired campaigns get lowest priority

        # Calculate time urgency score (0-100 scale)
        # Use a reasonable time window - campaigns ending within 72 hours are considered urgent
        max_urgency_window = 72  # hours
        time_urgency_score = max(0, 100 * (1 - (time_remaining_hours / max_urgency_window)))
        time_urgency_score = min(100, time_urgency_score)  # Cap at 100

        # Priority component (0-100 scale)
        # Normalize user priority to 0-100 scale
        max_expected_priority = max(10, user_priority)  # Ensure we don't over-normalize
        priority_score = (user_priority / max_expected_priority) * 100

        # Weighted blend: 60% priority + 40% time urgency
        priority_weight = 0.60
        time_weight = 0.40

        blended_score = (priority_weight * priority_score) + (time_weight * time_urgency_score)

        # Scale final score to maintain reasonable range relative to user priorities
        final_score = (blended_score / 100) * user_priority + (blended_score * 0.1)

        return final_score

    def _calculate_smart_priority(self, campaign: DropsCampaign, user_priority: int) -> float:
        """
        Calculate smart priority score that ensures higher priority games complete before lower ones.

        Args:
            campaign: The campaign to calculate priority for
            user_priority: User-defined priority value (higher = more important)

        Returns:
            Smart priority score (higher = more important)
        """
        current_time = datetime.now(timezone.utc)
        time_remaining_hours = (campaign.ends_at - current_time).total_seconds() / 3600

        if time_remaining_hours <= 0:
            return -float('inf')  # Expired campaigns get lowest priority

        # Calculate time pressure
        minutes_needed = campaign.remaining_minutes
        hours_needed = minutes_needed / 60

        # Calculate completion risk (0-1, where 1 = very risky)
        # Risk factor: how tight is the time window?
        buffer_factor = 1.2  # 20% buffer
        time_risk = max(0, 1 - (time_remaining_hours / (hours_needed * buffer_factor))) if hours_needed > 0 else 0

        # Calculate priority boost for high-priority games at risk
        # Higher priority games get bigger boost when at risk
        priority_boost = user_priority * time_risk * 10  # Scale factor
        final_score = user_priority + priority_boost

        return final_score

    async def fetch_inventory(self) -> None:
        """Fetch and process the drops inventory."""
        status_update = self.twitch.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))

        # fetch in-progress campaigns (inventory)
        response = await self.twitch.gql_request(GQL_OPERATIONS["Inventory"])
        inventory: JsonType = response["data"]["currentUser"]["inventory"]
        ongoing_campaigns: list[JsonType] = inventory["dropCampaignsInProgress"] or []

        # this contains claimed benefit edge IDs, not drop IDs
        claimed_benefits: dict[str, datetime] = {
            b["id"]: timestamp(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]
        }
        inventory_data: dict[str, JsonType] = {c["id"]: c for c in ongoing_campaigns}

        # fetch general available campaigns data (campaigns)
        response = await self.twitch.gql_request(GQL_OPERATIONS["Campaigns"])
        available_list: list[JsonType] = response["data"]["currentUser"]["dropCampaigns"] or []
        applicable_statuses = ("ACTIVE", "UPCOMING")
        available_campaigns: dict[str, JsonType] = {
            c["id"]: c
            for c in available_list
            if c["status"] in applicable_statuses  # that are currently not expired
        }

        # fetch detailed data for each campaign, in chunks
        # specifically use an intermediate list per a Python bug
        # https://github.com/python/cpython/issues/88342
        status_update(_("gui", "status", "fetching_campaigns"))
        for chunk_coro in asyncio.as_completed(
            [
                self.fetch_campaigns(campaigns_chunk)
                for campaigns_chunk in chunk(available_campaigns.items(), 20)
            ]
        ):
            chunk_campaigns_data = await chunk_coro
            # merge the inventory and campaigns datas together
            inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)

        # use the merged data to create campaign objects
        campaigns: list[DropsCampaign] = [
            DropsCampaign(self.twitch, campaign_data, claimed_benefits)
            for campaign_data in inventory_data.values()
        ]
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.linked, reverse=True)
        if self.twitch.settings.priority_algorithm == PRIORITY_ALGORITHM_ENDING_SOONEST:
            campaigns.sort(key=lambda c: c.ends_at)

        self._drops.clear()
        self.twitch.gui.inv.clear()
        self.inventory.clear()
        switch_triggers: set[datetime] = set()
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

        for i, campaign in enumerate(campaigns, start=1):
            status_update(
                _("gui", "status", "adding_campaigns").format(counter=f"({i}/{len(campaigns)})")
            )
            self._drops.update({drop.id: drop for drop in campaign.drops})
            if campaign.can_earn_within(next_hour):
                switch_triggers.update(campaign.time_triggers)
            # NOTE: this fetches pictures from the CDN, so might be slow without a cache
            await self.twitch.gui.inv.add_campaign(campaign)
            # this is needed here explicitly, because images aren't always fetched
            if self.twitch.gui.close_requested:
                raise ExitRequest()
            self.inventory.append(campaign)

        self._mnt_triggers.clear()
        self._mnt_triggers.extend(sorted(switch_triggers))
        # trim out all triggers that we're already past
        now = datetime.now(timezone.utc)
        while self._mnt_triggers and self._mnt_triggers[0] <= now:
            self._mnt_triggers.pop(0)

        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self.twitch._mnt_task is not None and not self.twitch._mnt_task.done():
            self.twitch._mnt_task.cancel()
        self.twitch._mnt_task = asyncio.create_task(self.twitch._maintenance_task())

    def get_active_drop(self, channel=None) -> TimedDrop | None:
        """Get the currently active drop for a channel."""
        if not self.twitch.wanted_games:
            return None
        watching_channel = self.twitch.watching_channel.get_with_default(channel)
        if watching_channel is None:
            # if we aren't watching anything, we can't earn any drops
            return None
        watching_game: Game | None = watching_channel.game
        if watching_game is None:
            # if the channel isn't playing anything in particular, we can't determine the drop
            return None
        drops: list[TimedDrop] = []
        for campaign in self.inventory:
            if (
                campaign.game == watching_game  # campaign's game matches watching game
                and campaign.can_earn(watching_channel)  # can be earned on this channel
            ):
                # add only the drops we can actually earn
                drops.extend(drop for drop in campaign.drops if drop.can_earn(watching_channel))
        if drops:
            drops.sort(key=lambda d: d.remaining_minutes)
            return drops[0]
        return None

    def update_drop_progress(self, drop_id: str, current_minutes: int) -> None:
        """Update drop progress from websocket or GQL."""
        if drop := self._drops.get(drop_id):
            drop.update_minutes(current_minutes)
            drop.display()

    def claim_drop(self, drop_id: str, drop_instance_id: str) -> bool:
        """Claim a drop and return success status."""
        if drop := self._drops.get(drop_id):
            drop.update_claim(drop_instance_id)
            return drop.claim()
        return False

    def get_drop(self, drop_id: str) -> TimedDrop | None:
        """Get a drop by ID."""
        return self._drops.get(drop_id)

    def clear_drops(self) -> None:
        """Clear all drops."""
        self._drops.clear()

    def get_maintenance_triggers(self) -> list[datetime]:
        """Get maintenance triggers for campaign time management."""
        return self._mnt_triggers.copy()

    def clear_maintenance_triggers(self) -> None:
        """Clear maintenance triggers."""
        self._mnt_triggers.clear()
