from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

import aiohttp
from yarl import URL

from constants import JsonType

logger = logging.getLogger("TwitchDrops")


class SteamAPIError(Exception):
    """Exception raised for Steam API related errors."""
    pass


class SteamGame:
    """Represents a Steam game with relevant data for sorting."""

    def __init__(self, appid: int, name: str, playtime_forever: int = 0,
                 release_date: Optional[str] = None, rating: Optional[float] = None):
        self.appid = appid
        self.name = name
        self.playtime_forever = playtime_forever  # in minutes
        self.release_date = release_date
        self.rating = rating

    def __repr__(self) -> str:
        return f"SteamGame({self.appid}, {self.name}, {self.playtime_forever}min)"


class SteamAPIClient:
    """Client for interacting with Steam Web API."""

    BASE_URL = "https://api.steampowered.com"
    STORE_URL = "https://store.steampowered.com/api"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Any] = {}
        self._cache_timestamps: Dict[str, datetime] = {}
        self._cache_duration = 3600  # 1 hour cache

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _is_cache_valid(self, key: str) -> bool:
        """Check if cached data is still valid."""
        if key not in self._cache_timestamps:
            return False
        age = datetime.now(timezone.utc) - self._cache_timestamps[key]
        return age.total_seconds() < self._cache_duration

    async def _make_request(self, url: str, params: Optional[Dict[str, Any]] = None) -> JsonType:
        """Make HTTP request with caching."""
        cache_key = f"{url}:{str(params)}"

        # Check cache first
        if self._is_cache_valid(cache_key):
            logger.debug(f"Using cached data for {url}")
            return self._cache[cache_key]

        session = await self.get_session()
        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    # Cache the result
                    self._cache[cache_key] = data
                    self._cache_timestamps[cache_key] = datetime.now(timezone.utc)
                    return data
                elif response.status == 403:
                    raise SteamAPIError("Invalid Steam API key or insufficient permissions")
                elif response.status == 429:
                    raise SteamAPIError("Steam API rate limit exceeded")
                else:
                    raise SteamAPIError(f"Steam API request failed with status {response.status}")
        except aiohttp.ClientError as e:
            raise SteamAPIError(f"Network error: {e}")

    async def get_steam_id(self, vanity_url: str) -> Optional[str]:
        """Resolve vanity URL to Steam ID."""
        url = f"{self.BASE_URL}/ISteamUser/ResolveVanityURL/v0001/"
        params = {
            "key": self.api_key,
            "vanityurl": vanity_url
        }

        try:
            data = await self._make_request(url, params)
            if data.get("response", {}).get("success") == 1:
                return data["response"]["steamid"]
            return None
        except SteamAPIError:
            return None

    async def get_owned_games(self, steam_id: str) -> List[SteamGame]:
        """Get user's owned games with playtime data."""
        url = f"{self.BASE_URL}/IPlayerService/GetOwnedGames/v0001/"
        params = {
            "key": self.api_key,
            "steamid": steam_id,
            "include_appinfo": "1",
            "include_played_free_games": "1"
        }

        try:
            data = await self._make_request(url, params)
            games = []

            for game_data in data.get("response", {}).get("games", []):
                game = SteamGame(
                    appid=game_data["appid"],
                    name=game_data["name"],
                    playtime_forever=game_data.get("playtime_forever", 0)
                )
                games.append(game)

            return games
        except SteamAPIError as e:
            logger.error(f"Failed to get owned games: {e}")
            return []

    async def get_game_details(self, appid: int) -> Tuple[Optional[str], Optional[float]]:
        """Get game release date and rating from Steam Store API."""
        url = f"{self.STORE_URL}/appdetails"
        params = {
            "appids": appid,
            "cc": "us",
            "l": "english"
        }

        try:
            data = await self._make_request(url, params)
            app_data = data.get(str(appid), {}).get("data", {})

            # Extract release date
            release_date = None
            release_info = app_data.get("release_date", {})
            if release_info.get("date"):
                release_date = release_info["date"]

            # Extract rating (user score)
            rating = None
            metacritic = app_data.get("metacritic", {})
            if metacritic.get("score"):
                rating = float(metacritic["score"]) / 100.0  # Convert to 0-1 scale

            return release_date, rating
        except SteamAPIError as e:
            logger.debug(f"Failed to get game details for {appid}: {e}")
            return None, None

    async def enrich_games_with_details(self, games: List[SteamGame]) -> List[SteamGame]:
        """Enrich games with release date and rating information."""
        # Process games in batches to avoid overwhelming the API
        batch_size = 10
        enriched_games = []

        for i in range(0, len(games), batch_size):
            batch = games[i:i + batch_size]
            tasks = []

            for game in batch:
                task = self.get_game_details(game.appid)
                tasks.append(task)

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for game, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.debug(f"Failed to enrich {game.name}: {result}")
                    enriched_games.append(game)
                else:
                    release_date, rating = result
                    game.release_date = release_date
                    game.rating = rating
                    enriched_games.append(game)

            # Small delay between batches to be respectful to the API
            if i + batch_size < len(games):
                await asyncio.sleep(0.5)

        return enriched_games

    async def get_user_games_data(self, steam_id: str) -> List[SteamGame]:
        """Get complete game data for a user (owned games + details)."""
        # Get owned games
        owned_games = await self.get_owned_games(steam_id)
        if not owned_games:
            return []

        # Enrich with additional details
        enriched_games = await self.enrich_games_with_details(owned_games)
        return enriched_games

    def sort_games_by_playtime(self, games: List[SteamGame], reverse: bool = True) -> List[SteamGame]:
        """Sort games by playtime (highest first by default)."""
        return sorted(games, key=lambda g: g.playtime_forever, reverse=reverse)

    def sort_games_by_release_date(self, games: List[SteamGame], reverse: bool = True) -> List[SteamGame]:
        """Sort games by release date (newest first by default)."""
        def get_date_key(game: SteamGame) -> datetime:
            if not game.release_date:
                return datetime.min.replace(tzinfo=timezone.utc)
            try:
                # Try to parse the date string
                return datetime.strptime(game.release_date, "%d %b, %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    return datetime.strptime(game.release_date, "%b %d, %Y").replace(tzinfo=timezone.utc)
                except ValueError:
                    return datetime.min.replace(tzinfo=timezone.utc)

        return sorted(games, key=get_date_key, reverse=reverse)

    def sort_games_by_rating(self, games: List[SteamGame], reverse: bool = True) -> List[SteamGame]:
        """Sort games by rating (highest first by default)."""
        def get_rating_key(game: SteamGame) -> float:
            return game.rating if game.rating is not None else 0.0

        return sorted(games, key=get_rating_key, reverse=reverse)
