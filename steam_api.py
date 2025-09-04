from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import aiohttp
from yarl import URL

from constants import JsonType, STEAM_CACHE_DB
from utils import json_load, json_save
from pathlib import Path

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
    CACHE_DURATION = timedelta(days=30)  # Cache Steam data for 30 days
    STEAM_CACHE_FILE = STEAM_CACHE_DB

    def __init__(self, api_key: str):
        print(f"Initializing SteamAPIClient with API key: {api_key[:8]}...")
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._memory_cache: Dict[str, Any] = {}
        self._memory_cache_timestamps: Dict[str, datetime] = {}
        self._memory_cache_duration = 3600  # 1 hour memory cache

        # Load persistent cache from steam_data.json
        self._load_persistent_cache()

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            # Create a connector with proper limits to avoid resource warnings
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            # Wait a moment for the session to fully close
            await asyncio.sleep(0.1)

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    def _load_persistent_cache(self):
        """Load Steam data cache from steam_data.json."""
        try:
            self._persistent_cache = json_load(self.STEAM_CACHE_FILE, {})
            print(f"Loaded {len(self._persistent_cache)} Steam data entries from cache")
        except Exception as e:
            print(f"Failed to load Steam cache: {e}")
            self._persistent_cache = {}

    def _save_persistent_cache(self):
        """Save Steam data cache to steam_data.json."""
        try:
            json_save(self.STEAM_CACHE_FILE, self._persistent_cache, sort=True)
            print(f"Saved {len(self._persistent_cache)} Steam data entries to cache")
        except Exception as e:
            print(f"Failed to save Steam cache: {e}")

    def _get_cache_key(self, steam_id: str, data_type: str) -> str:
        """Generate cache key for Steam data."""
        return f"steam_{steam_id}_{data_type}"

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached data is still valid."""
        if cache_key not in self._persistent_cache:
            return False

        cache_entry = self._persistent_cache[cache_key]
        cached_time = datetime.fromisoformat(cache_entry["timestamp"])
        age = datetime.now(timezone.utc) - cached_time
        return age < self.CACHE_DURATION

    def _get_cached_data(self, cache_key: str) -> Optional[Any]:
        """Get cached data if valid."""
        if self._is_cache_valid(cache_key):
            logger.debug(f"Using cached Steam data for {cache_key}")
            return self._persistent_cache[cache_key]["data"]
        return None

    def _cache_data(self, cache_key: str, data: Any):
        """Cache data with timestamp."""
        self._persistent_cache[cache_key] = {
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self._save_persistent_cache()

    def _is_memory_cache_valid(self, key: str) -> bool:
        """Check if memory cached data is still valid."""
        if key not in self._memory_cache_timestamps:
            return False
        age = datetime.now(timezone.utc) - self._memory_cache_timestamps[key]
        return age.total_seconds() < self._memory_cache_duration

    async def _make_request(self, url: str, params: Optional[Dict[str, Any]] = None) -> JsonType:
        """Make HTTP request with memory caching and timeout."""
        cache_key = f"{url}:{str(params)}"

        # Check memory cache first
        if self._is_memory_cache_valid(cache_key):
            print(f"Using memory cached data for {url}")
            return self._memory_cache[cache_key]

        session = await self.get_session()
        timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout

        try:
            async with session.get(url, params=params, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
                    # Cache the result in memory
                    self._memory_cache[cache_key] = data
                    self._memory_cache_timestamps[cache_key] = datetime.now(timezone.utc)
                    return data
                elif response.status == 403:
                    raise SteamAPIError("Invalid Steam API key or insufficient permissions")
                elif response.status == 429:
                    raise SteamAPIError("Steam API rate limit exceeded")
                else:
                    raise SteamAPIError(f"Steam API request failed with status {response.status}")
        except asyncio.TimeoutError:
            raise SteamAPIError("Steam API request timed out")
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

        print(f"Requesting owned games for Steam ID: {steam_id}")
        print(f"API URL: {url}")
        print(f"API Key: {self.api_key[:8]}...")
        print("Making Steam API request...")

        try:
            data = await self._make_request(url, params)
            # Don't print the full response as it may contain Unicode characters that cause encoding issues

            games = []
            response_data = data.get("response", {})

            if "games" not in response_data:
                print(f"No games found in response: {response_data}")
                return []

            for game_data in response_data.get("games", []):
                game = SteamGame(
                    appid=game_data["appid"],
                    name=game_data["name"],
                    playtime_forever=game_data.get("playtime_forever", 0)
                )
                games.append(game)

            print(f"Retrieved {len(games)} owned games")
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

    async def get_user_games_data(self, steam_id: str, priority_games: Optional[List[str]] = None) -> List[SteamGame]:
        """Get complete game data for a user (owned games + details) with persistent caching.

        Args:
            steam_id: Steam user ID
            priority_games: Optional list of game names to filter by. If provided, only these games will be enriched.
        """
        if priority_games:
            print(f"Fetching Steam data for {len(priority_games)} priority games")
            return await self._get_priority_games_data(steam_id, priority_games)

        # Legacy behavior - get all games (kept for compatibility)
        cache_key = self._get_cache_key(steam_id, "games_data")

        # Check persistent cache first
        cached_data = self._get_cached_data(cache_key)
        if cached_data is not None:
            print(f"Using cached Steam games data for user {steam_id}")
            # Convert cached data back to SteamGame objects
            games = []
            for game_data in cached_data:
                game = SteamGame(
                    appid=game_data["appid"],
                    name=game_data["name"],
                    playtime_forever=game_data["playtime_forever"],
                    release_date=game_data.get("release_date"),
                    rating=game_data.get("rating")
                )
                games.append(game)
            return games

        print(f"Fetching fresh Steam games data for user {steam_id}")

        # Get owned games
        owned_games = await self.get_owned_games(steam_id)
        if not owned_games:
            return []

        # Enrich with additional details
        enriched_games = await self.enrich_games_with_details(owned_games)

        # Cache the results
        cache_data = []
        for game in enriched_games:
            cache_data.append({
                "appid": game.appid,
                "name": game.name,
                "playtime_forever": game.playtime_forever,
                "release_date": game.release_date,
                "rating": game.rating
            })

        self._cache_data(cache_key, cache_data)
        print(f"Cached Steam games data for user {steam_id}")

        return enriched_games

    async def _get_priority_games_data(self, steam_id: str, priority_games: List[str]) -> List[SteamGame]:
        """Get Steam data for specific priority games only."""
        # Check individual game cache first
        results = []
        uncached_games = []

        for game_name in priority_games:
            cache_key = self._get_cache_key(steam_id, f"game_{game_name.lower()}")
            cached_data = self._get_cached_data(cache_key)

            if cached_data is not None:
                game = SteamGame(
                    appid=cached_data["appid"],
                    name=cached_data["name"],
                    playtime_forever=cached_data["playtime_forever"],
                    release_date=cached_data.get("release_date"),
                    rating=cached_data.get("rating")
                )
                results.append(game)
                print(f"Using cached data for {game_name}")
            else:
                uncached_games.append(game_name)

        if not uncached_games:
            print(f"All {len(priority_games)} priority games found in cache")
            return results

        print(f"Fetching Steam data for {len(uncached_games)} uncached priority games")

        # Get all owned games (fast - just names and IDs)
        owned_games = await self.get_owned_games(steam_id)
        if not owned_games:
            return results

        # Create mapping for fast lookup
        owned_games_map = {game.name.lower(): game for game in owned_games}

        # Filter to only uncached priority games that user owns
        priority_owned_games = []
        for game_name in uncached_games:
            owned_game = owned_games_map.get(game_name.lower())
            if owned_game:
                priority_owned_games.append(owned_game)
                print(f"Found owned game: {game_name}")
            else:
                print(f"Game not owned on Steam: {game_name}")

        if not priority_owned_games:
            print("No priority games found in Steam library")
            return results

        print(f"Enriching {len(priority_owned_games)} priority games with details")
        # Enrich only the priority games with details
        enriched_games = await self.enrich_games_with_details(priority_owned_games)

        # Cache individual games and add to results
        for game in enriched_games:
            cache_key = self._get_cache_key(steam_id, f"game_{game.name.lower()}")
            cache_data = {
                "appid": game.appid,
                "name": game.name,
                "playtime_forever": game.playtime_forever,
                "release_date": game.release_date,
                "rating": game.rating
            }
            self._cache_data(cache_key, cache_data)
            results.append(game)

        print(f"Successfully fetched data for {len(enriched_games)} priority games")
        return results

    def clear_steam_cache(self, steam_id: Optional[str] = None):
        """Clear Steam data cache for a specific user or all users."""
        if steam_id:
            # Clear cache for specific user
            cache_key = self._get_cache_key(steam_id, "games_data")
            if cache_key in self._persistent_cache:
                del self._persistent_cache[cache_key]
                self._save_persistent_cache()
                logger.info(f"Cleared Steam cache for user {steam_id}")
        else:
            # Clear all Steam cache
            self._persistent_cache.clear()
            self._save_persistent_cache()
            logger.info("Cleared all Steam cache data")

    def delete_cache_file(self):
        """Delete the Steam cache file completely."""
        try:
            if self.STEAM_CACHE_FILE.exists():
                self.STEAM_CACHE_FILE.unlink()
                logger.info(f"Deleted Steam cache file: {self.STEAM_CACHE_FILE}")
            self._persistent_cache.clear()
        except Exception as e:
            logger.warning(f"Failed to delete Steam cache file: {e}")

    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about cached Steam data."""
        now = datetime.now(timezone.utc)
        cache_info = {
            "cache_file": str(self.STEAM_CACHE_FILE),
            "total_entries": len(self._persistent_cache),
            "users": [],
            "oldest_entry": None,
            "newest_entry": None
        }

        timestamps = []
        for cache_key, cache_entry in self._persistent_cache.items():
            if cache_key.startswith("steam_") and cache_key.endswith("_games_data"):
                steam_id = cache_key.replace("steam_", "").replace("_games_data", "")
                cached_time = datetime.fromisoformat(cache_entry["timestamp"])
                age = now - cached_time

                cache_info["users"].append({
                    "steam_id": steam_id,
                    "cached_at": cached_time.isoformat(),
                    "age_days": age.days,
                    "is_valid": age < self.CACHE_DURATION,
                    "games_count": len(cache_entry["data"]) if "data" in cache_entry else 0
                })
                timestamps.append(cached_time)

        if timestamps:
            cache_info["oldest_entry"] = min(timestamps).isoformat()
            cache_info["newest_entry"] = max(timestamps).isoformat()

        return cache_info

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
