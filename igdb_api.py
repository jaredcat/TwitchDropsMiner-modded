from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import aiohttp
from yarl import URL

from constants import JsonType, IGDB_CACHE_DB
from utils import json_load, json_save

logger = logging.getLogger("TwitchDrops")


class IGDBAPIError(Exception):
    """Exception raised for IGDB API related errors."""
    pass


class IGDBGame:
    """Represents an IGDB game with relevant data for sorting."""

    def __init__(self, igdb_id: int, name: str, release_date: Optional[str] = None, rating: Optional[float] = None):
        self.igdb_id = igdb_id
        self.name = name
        self.release_date = release_date  # ISO format: "2023-01-01"
        self.rating = rating  # 0-100 scale

    def __repr__(self) -> str:
        return f"IGDBGame({self.igdb_id}, {self.name})"


class IGDBAPIClient:
    """Client for interacting with IGDB API."""

    BASE_URL = "https://api.igdb.com/v4"
    CACHE_DURATION = timedelta(days=30)  # Cache IGDB data for 30 days
    IGDB_CACHE_FILE = IGDB_CACHE_DB


    def __init__(self, client_id: str, client_secret: str):
        print(f"Initializing IGDBAPIClient with client ID: {client_id[:8]}...")
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._memory_cache: Dict[str, Any] = {}
        self._memory_cache_timestamps: Dict[str, datetime] = {}
        self._memory_cache_duration = 3600  # 1 hour memory cache

        # Load persistent cache from igdb_data.json
        self._load_persistent_cache()

    async def _get_access_token(self) -> str:
        """Get access token using client credentials."""
        if self.access_token:
            return self.access_token

        print("Getting IGDB access token...")
        async with aiohttp.ClientSession() as session:
            data = {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'grant_type': 'client_credentials'
            }

            async with session.post('https://id.twitch.tv/oauth2/token', data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    self.access_token = result['access_token']
                    print("IGDB access token obtained successfully")
                    return self.access_token
                else:
                    error_text = await response.text()
                    raise IGDBAPIError(f"Failed to get access token: {response.status} - {error_text}")

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            # Get access token first
            access_token = await self._get_access_token()

            # Create a connector with proper limits to avoid resource warnings
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            headers = {
                'Client-ID': self.client_id,
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json'
            }
            self._session = aiohttp.ClientSession(connector=connector, headers=headers)
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
        """Load IGDB data cache from igdb_data.json."""
        try:
            self._persistent_cache = json_load(self.IGDB_CACHE_FILE, {})
            print(f"Loaded {len(self._persistent_cache)} IGDB data entries from cache")
        except Exception as e:
            print(f"Failed to load IGDB cache: {e}")
            self._persistent_cache = {}

    def _save_persistent_cache(self):
        """Save IGDB data cache to igdb_data.json."""
        try:
            json_save(self.IGDB_CACHE_FILE, self._persistent_cache, sort=True)
            print(f"Saved {len(self._persistent_cache)} IGDB data entries to cache")
        except Exception as e:
            print(f"Failed to save IGDB cache: {e}")

    def _get_cache_key(self, data_type: str) -> str:
        """Generate cache key for IGDB data."""
        return f"igdb_{data_type}"

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
            logger.debug(f"Using cached IGDB data for {cache_key}")
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

    async def _make_request(self, endpoint: str, query: str) -> JsonType:
        """Make HTTP request to IGDB API with memory caching and timeout."""
        cache_key = f"{endpoint}:{query}"

        # Check memory cache first
        if self._is_memory_cache_valid(cache_key):
            print(f"Using memory cached data for {endpoint}")
            return self._memory_cache[cache_key]

        session = await self.get_session()
        timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout

        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "text/plain"
        }

        try:
            async with session.post(f"{self.BASE_URL}/{endpoint}", data=query, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    data = await response.json()
                    # Cache the result in memory
                    self._memory_cache[cache_key] = data
                    self._memory_cache_timestamps[cache_key] = datetime.now(timezone.utc)
                    return data
                elif response.status == 401:
                    raise IGDBAPIError("Invalid IGDB API credentials")
                elif response.status == 429:
                    raise IGDBAPIError("IGDB API rate limit exceeded")
                else:
                    raise IGDBAPIError(f"IGDB API request failed with status {response.status}")
        except asyncio.TimeoutError:
            raise IGDBAPIError("IGDB API request timed out")
        except aiohttp.ClientError as e:
            raise IGDBAPIError(f"Network error: {e}")

    async def get_games_data(self, game_ids: List[int]) -> List[IGDBGame]:
        """Get game data for specific IGDB game IDs."""
        if not game_ids:
            return []

        # Check cache first
        cache_key = self._get_cache_key("games_data")
        cached_data = self._get_cached_data(cache_key)

        if cached_data is not None:
            # Filter cached data to only include requested game IDs
            cached_games = []
            for game_data in cached_data:
                if game_data["id"] in game_ids:
                    cached_games.append(game_data)

            if len(cached_games) == len(game_ids):
                print(f"Using cached IGDB data for {len(game_ids)} games")
                return [self._create_game_from_data(game_data) for game_data in cached_games]

        print(f"Fetching IGDB data for {len(game_ids)} games")

        # Build query for IGDB API
        ids_str = ",".join(map(str, game_ids))
        query = f"""
        fields id,name,first_release_date,rating,rating_count;
        where id = ({ids_str});
        limit {len(game_ids)};
        """

        try:
            data = await self._make_request("games", query)

            games = []
            for game_data in data:
                game = self._create_game_from_data(game_data)
                games.append(game)

            # Cache the results
            self._cache_data(cache_key, data)
            print(f"Cached IGDB data for {len(games)} games")

            return games

        except IGDBAPIError as e:
            logger.error(f"Failed to get IGDB games data: {e}")
            return []

    def _create_game_from_data(self, game_data: Dict[str, Any]) -> IGDBGame:
        """Create IGDBGame object from API response data."""
        igdb_id = game_data["id"]
        name = game_data["name"]

        # Parse release date (Unix timestamp)
        release_date = None
        if "first_release_date" in game_data and game_data["first_release_date"]:
            try:
                # Convert Unix timestamp to ISO date string
                timestamp = game_data["first_release_date"]
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                release_date = dt.strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass

        # Parse rating (0-100 scale)
        rating = None
        if "rating" in game_data and game_data["rating"] is not None:
            try:
                rating = float(game_data["rating"])
            except (ValueError, TypeError):
                pass

        return IGDBGame(igdb_id, name, release_date, rating)

    def sort_games_by_release_date(self, games: List[IGDBGame], reverse: bool = False) -> List[IGDBGame]:
        """Sort games by release date (oldest first by default)."""
        def get_date_key(game: IGDBGame) -> datetime:
            if not game.release_date:
                return datetime.max.replace(tzinfo=timezone.utc)
            try:
                return datetime.fromisoformat(game.release_date).replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.max.replace(tzinfo=timezone.utc)

        return sorted(games, key=get_date_key, reverse=reverse)

    def sort_games_by_rating(self, games: List[IGDBGame], reverse: bool = True) -> List[IGDBGame]:
        """Sort games by rating (highest first by default)."""
        def get_rating_key(game: IGDBGame) -> float:
            return game.rating if game.rating is not None else 0.0

        return sorted(games, key=get_rating_key, reverse=reverse)

    def clear_igdb_cache(self):
        """Clear IGDB data cache."""
        self._persistent_cache.clear()
        self._save_persistent_cache()
        logger.info("Cleared all IGDB cache data")

    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about cached IGDB data."""
        now = datetime.now(timezone.utc)
        cache_info = {
            "cache_file": str(self.IGDB_CACHE_FILE),
            "total_entries": len(self._persistent_cache),
            "oldest_entry": None,
            "newest_entry": None
        }

        timestamps = []
        for cache_key, cache_entry in self._persistent_cache.items():
            if cache_key.startswith("igdb_"):
                cached_time = datetime.fromisoformat(cache_entry["timestamp"])
                age = now - cached_time
                timestamps.append(cached_time)

        if timestamps:
            cache_info["oldest_entry"] = min(timestamps).isoformat()
            cache_info["newest_entry"] = max(timestamps).isoformat()

        return cache_info

    async def sort_games_by_release_date(self, game_names: List[str], twitch_games: Dict) -> List[str]:
        """Sort game names by IGDB release date."""
        if not game_names:
            return game_names

        # Get game IDs from Twitch data
        game_ids = []
        for game_name in game_names:
            for game in twitch_games.keys():
                if game.name == game_name:
                    game_ids.append(game.id)
                    break

        if not game_ids:
            print("No IGDB game IDs found - keeping original order")
            return game_names

        # Get IGDB data
        games_data = await self.get_games_data(game_ids)
        if not games_data:
            print("No IGDB data available - keeping original order")
            return game_names

        # Create mapping of game names to IGDB data
        igdb_data = {}
        for game in games_data:
            for twitch_game in twitch_games.keys():
                if twitch_game.id == game.igdb_id:
                    igdb_data[twitch_game.name] = {
                        "release_date": game.release_date,
                        "rating": game.rating
                    }
                    break

        # Sort by release date
        def get_release_date(game_name):
            game_data = igdb_data.get(game_name, {})
            release_date = game_data.get("release_date")
            if not release_date:
                return "9999-12-31"  # Put games without dates at the end
            return release_date

        return sorted(game_names, key=get_release_date)

    async def sort_games_by_rating(self, game_names: List[str], twitch_games: Dict) -> List[str]:
        """Sort game names by IGDB rating (highest first)."""
        if not game_names:
            return game_names

        # Get game IDs from Twitch data
        game_ids = []
        for game_name in game_names:
            for game in twitch_games.keys():
                if game.name == game_name:
                    game_ids.append(game.id)
                    break

        if not game_ids:
            print("No IGDB game IDs found - keeping original order")
            return game_names

        # Get IGDB data
        games_data = await self.get_games_data(game_ids)
        if not games_data:
            print("No IGDB data available - keeping original order")
            return game_names

        # Create mapping of game names to IGDB data
        igdb_data = {}
        for game in games_data:
            for twitch_game in twitch_games.keys():
                if twitch_game.id == game.igdb_id:
                    igdb_data[twitch_game.name] = {
                        "release_date": game.release_date,
                        "rating": game.rating
                    }
                    break

        # Sort by rating (highest first)
        def get_rating(game_name):
            game_data = igdb_data.get(game_name, {})
            rating = game_data.get("rating")
            if rating is None:
                return 0.0  # Games without ratings go to end
            return rating

        return sorted(game_names, key=get_rating, reverse=True)
