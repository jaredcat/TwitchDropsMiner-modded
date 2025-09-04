from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import aiohttp

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

    def _cache_data(self, cache_key: str, data: Any, search_term: str = None):
        """Cache data with timestamp and optional search term."""
        cache_entry = {
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        if search_term:
            cache_entry["search_term"] = search_term
        self._persistent_cache[cache_key] = cache_entry
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

        print(f"Making IGDB API request to {endpoint}")
        print(f"Query: {query[:100]}{'...' if len(query) > 100 else ''}")
        print(f"Headers: Client-ID={self.client_id[:8]}..., Authorization=Bearer {self.access_token[:20]}...")

        try:
            async with session.post(f"{self.BASE_URL}/{endpoint}", data=query, headers=headers, timeout=timeout) as response:
                print(f"IGDB API response status: {response.status}")
                if response.status == 200:
                    data = await response.json()
                    print(f"IGDB API returned {len(data)} results")
                    # Cache the result in memory
                    self._memory_cache[cache_key] = data
                    self._memory_cache_timestamps[cache_key] = datetime.now(timezone.utc)
                    return data
                elif response.status == 401:
                    error_text = await response.text()
                    print(f"IGDB API authentication failed: {error_text}")
                    raise IGDBAPIError("Invalid IGDB API credentials")
                elif response.status == 429:
                    error_text = await response.text()
                    print(f"IGDB API rate limit exceeded: {error_text}")
                    raise IGDBAPIError("IGDB API rate limit exceeded")
                else:
                    error_text = await response.text()
                    print(f"IGDB API request failed: {response.status} - {error_text}")
                    raise IGDBAPIError(f"IGDB API request failed with status {response.status}")
        except asyncio.TimeoutError:
            print("IGDB API request timed out")
            raise IGDBAPIError("IGDB API request timed out")
        except aiohttp.ClientError as e:
            print(f"IGDB API network error: {e}")
            raise IGDBAPIError(f"Network error: {e}")

    async def get_games_data(self, game_ids: List[int], game_names: List[str] = None) -> List[IGDBGame]:
        """Get game data for specific IGDB game IDs."""
        if not game_ids:
            return []

        print(f"Requesting IGDB data for {len(game_ids)} games: {game_ids[:10]}{'...' if len(game_ids) > 10 else ''}")

        # Create mapping from game ID to name if provided
        id_to_name = {}
        if game_names and len(game_names) == len(game_ids):
            id_to_name = dict(zip(game_ids, game_names))

        # Check individual game cache first
        results = []
        uncached_ids = []

        for game_id in game_ids:
            cache_key = self._get_cache_key(f"game_{game_id}")
            cached_data = self._get_cached_data(cache_key)

            if cached_data is not None:
                game = self._create_game_from_data(cached_data)
                results.append(game)
                print(f"Using cached data for game ID {game_id}")
            else:
                uncached_ids.append(game_id)

        if not uncached_ids:
            print(f"All {len(game_ids)} games found in cache")
            return results

        print(f"Fetching IGDB data for {len(uncached_ids)} uncached games")

        # Process in batches to avoid API limits
        batch_size = 100  # IGDB allows up to 500
        all_games = results.copy()

        for i in range(0, len(uncached_ids), batch_size):
            batch_ids = uncached_ids[i:i + batch_size]
            print(f"Processing batch {i//batch_size + 1}: {len(batch_ids)} games")

            # Build query for IGDB API
            ids_str = ",".join(map(str, batch_ids))
            query = f"""
            fields id,name,first_release_date,rating,rating_count;
            where id = ({ids_str});
            limit {len(batch_ids)};
            """

            try:
                data = await self._make_request("games", query)
                print(f"IGDB API returned {len(data)} games for batch")

                for game_data in data:
                    game = self._create_game_from_data(game_data)
                    all_games.append(game)

                    # Cache individual game with search term if available
                    cache_key = self._get_cache_key(f"game_{game_data['id']}")
                    search_term = id_to_name.get(game_data['id'])
                    self._cache_data(cache_key, game_data, search_term=search_term)

                # Small delay between batches to be respectful to the API
                if i + batch_size < len(uncached_ids):
                    await asyncio.sleep(0.5)

            except IGDBAPIError as e:
                print(f"Failed to get IGDB games data for batch: {e}")
                continue

        print(f"Successfully processed {len(all_games)} games total")
        return all_games

    async def search_games_by_names(self, game_names: List[str]) -> List[IGDBGame]:
        """Search IGDB for games by their names."""
        if not game_names:
            return []

        print(f"Searching IGDB for {len(game_names)} games by name")

        # Check cache first for any games we already have
        all_games = []
        games_to_search = []

        for name in game_names:
            # Check if we already have this game cached by searching through all cached games
            found_in_cache = False
            for cache_key, cache_entry in self._persistent_cache.items():
                if cache_key.startswith("igdb_game_") and cache_entry.get("search_term") == name:
                    # Found cached game for this search term
                    cached_data = cache_entry["data"]
                    game = self._create_game_from_data(cached_data)
                    all_games.append(game)
                    found_in_cache = True
                    print(f"Using cached data for '{name}' (ID: {game.igdb_id})")
                    break

            if not found_in_cache:
                games_to_search.append(name)

        if not games_to_search:
            print(f"All {len(game_names)} games found in cache")
            return all_games

        print(f"Found {len(all_games)} games in cache, searching IGDB for {len(games_to_search)} games")

        # Process remaining games in batches to avoid API limits
        batch_size = 50  # Smaller batches for name searches

        for i in range(0, len(games_to_search), batch_size):
            batch_names = games_to_search[i:i + batch_size]
            print(f"Processing name batch {i//batch_size + 1}: {len(batch_names)} games")

            # Search for each game individually to get better matches
            for name in batch_names:
                # Escape special characters and add fuzzy matching
                escaped_name = name.replace('"', '\\"')

                query = f"""
                fields id,name,first_release_date,rating,rating_count;
                search "{escaped_name}";
                where name ~ "{escaped_name}"*;
                limit 1;
                """

                try:
                    data = await self._make_request("games", query)
                    print(f"IGDB search for '{name}' returned {len(data)} results")

                    if data:
                        # Only use the first (best) result and cache it
                        best_match = data[0]
                        game = self._create_game_from_data(best_match)
                        all_games.append(game)

                        # Cache only the best match with search term
                        cache_key = self._get_cache_key(f"game_{best_match['id']}")
                        self._cache_data(cache_key, best_match, search_term=name)
                        print(f"Using best match: '{game.name}' (ID: {game.igdb_id})")

                    # Small delay between individual searches
                    await asyncio.sleep(0.1)

                except IGDBAPIError as e:
                    print(f"Failed to search IGDB for '{name}': {e}")
                    continue

            # Small delay between batches
            if i + batch_size < len(games_to_search):
                await asyncio.sleep(0.5)

        print(f"Successfully found {len(all_games)} games total")
        return all_games

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


    async def get_popularity_data(self, game_ids: List[int]) -> Dict[int, int]:
        """Get popularity data for specific game IDs from popularity_primitives endpoint."""
        if not game_ids:
            return {}

        print(f"Fetching popularity data for {len(game_ids)} games")

        # Batch process game IDs
        batch_size = 500
        popularity_data = {}

        for i in range(0, len(game_ids), batch_size):
            batch_ids = game_ids[i:i + batch_size]
            print(f"Processing popularity batch {i//batch_size + 1}: {len(batch_ids)} games")

            # Build query for popularity_primitives endpoint
            ids_str = ",".join(map(str, batch_ids))
            query = f"""
            fields game_id,value,popularity_type;
            where game_id = ({ids_str}) & popularity_type = 1;
            sort value desc;
            limit {len(batch_ids)};
            """

            try:
                data = await self._make_request("popularity_primitives", query)
                print(f"Popularity API returned {len(data)} entries for batch")

                for entry in data:
                    game_id = entry.get("game_id")
                    value = entry.get("value", 0)
                    if game_id is not None:
                        popularity_data[game_id] = value

                # Small delay between batches
                if i + batch_size < len(game_ids):
                    await asyncio.sleep(0.5)

            except IGDBAPIError as e:
                print(f"Failed to fetch popularity data for batch: {e}")
                continue

        print(f"Retrieved popularity data for {len(popularity_data)} games")
        return popularity_data

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
                timestamps.append(cached_time)

        if timestamps:
            cache_info["oldest_entry"] = min(timestamps).isoformat()
            cache_info["newest_entry"] = max(timestamps).isoformat()

        return cache_info

    async def sort_games_by_release_date(self, game_names: List[str], twitch_games: Dict) -> List[str]:
        """Sort game names by IGDB release date."""
        if not game_names:
            return game_names

        print(f"Sorting {len(game_names)} games by release date")
        print(f"Available Twitch games: {len(twitch_games)}")

        # Debug: Show what Twitch games we have
        if twitch_games:
            print("Twitch games available:")
            for game, priority in list(twitch_games.items())[:5]:  # Show first 5
                print(f"  - {game.name} (ID: {game.id}, Priority: {priority})")
            if len(twitch_games) > 5:
                print(f"  ... and {len(twitch_games) - 5} more")

        # First try to get IGDB IDs from Twitch drops
        game_ids = []
        found_by_id = []
        for game_name in game_names:
            found = False
            for game in twitch_games.keys():
                if game.name == game_name:
                    game_ids.append(game.id)
                    found_by_id.append(game_name)
                    found = True
                    break
            if not found:
                print(f"Game not found in Twitch drops: {game_name}")

        print(f"Found {len(game_ids)} games in Twitch drops, {len(game_names) - len(game_ids)} need name search")

        # Get IGDB data for games found in drops
        games_data = []
        if game_ids:
            print(f"Getting IGDB data for {len(game_ids)} games by ID: {game_ids[:5]}{'...' if len(game_ids) > 5 else ''}")
            # Pass the corresponding game names for better caching
            found_game_names = [name for name in found_by_id]
            games_data = await self.get_games_data(game_ids, found_game_names)
            print(f"Retrieved IGDB data for {len(games_data)} games from drops")

        # For games not found in drops, search by name
        games_not_found = [name for name in game_names if name not in found_by_id]
        if games_not_found:
            print(f"Searching IGDB by name for {len(games_not_found)} games")
            name_search_results = await self.search_games_by_names(games_not_found)
            games_data.extend(name_search_results)
            print(f"Found {len(name_search_results)} additional games by name search")

        if not games_data:
            print("No IGDB data available - keeping original order")
            return game_names

        # Create mapping of game names to IGDB data
        igdb_data = {}
        for game in games_data:
            igdb_data[game.name] = {
                "release_date": game.release_date,
                "rating": game.rating
            }

        print(f"Found IGDB data for {len(igdb_data)} games total")

        # Sort by release date
        def get_release_date(game_name):
            game_data = igdb_data.get(game_name, {})
            release_date = game_data.get("release_date")
            if not release_date:
                return "9999-12-31"  # Put games without dates at the end
            return release_date

        return sorted(game_names, key=get_release_date, reverse=True)  # Newest first

    async def sort_games_by_rating(self, game_names: List[str], twitch_games: Dict) -> List[str]:
        """Sort game names by IGDB rating (highest first)."""
        if not game_names:
            return game_names

        print(f"Sorting {len(game_names)} games by rating")

        # First try to get IGDB IDs from Twitch drops
        game_ids = []
        found_by_id = []
        for game_name in game_names:
            found = False
            for game in twitch_games.keys():
                if game.name == game_name:
                    game_ids.append(game.id)
                    found_by_id.append(game_name)
                    found = True
                    break
            if not found:
                print(f"Game not found in Twitch drops: {game_name}")

        print(f"Found {len(game_ids)} games in Twitch drops, {len(game_names) - len(game_ids)} need name search")

        # Get IGDB data for games found in drops
        games_data = []
        if game_ids:
            # Pass the corresponding game names for better caching
            found_game_names = [name for name in found_by_id]
            games_data = await self.get_games_data(game_ids, found_game_names)
            print(f"Retrieved IGDB data for {len(games_data)} games from drops")

        # For games not found in drops, search by name
        games_not_found = [name for name in game_names if name not in found_by_id]
        if games_not_found:
            print(f"Searching IGDB by name for {len(games_not_found)} games")
            name_search_results = await self.search_games_by_names(games_not_found)
            games_data.extend(name_search_results)
            print(f"Found {len(name_search_results)} additional games by name search")

        if not games_data:
            print("No IGDB data available - keeping original order")
            return game_names

        # Create mapping of game names to IGDB data
        igdb_data = {}
        for game in games_data:
            igdb_data[game.name] = {
                "release_date": game.release_date,
                "rating": game.rating
            }

        print(f"Found IGDB data for {len(igdb_data)} games total")

        # Sort by rating (highest first)
        def get_rating(game_name):
            game_data = igdb_data.get(game_name, {})
            rating = game_data.get("rating")
            if rating is None:
                return 0.0  # Games without ratings go to end
            return rating

        return sorted(game_names, key=get_rating, reverse=True)

    async def sort_games_by_popularity(self, game_names: List[str], twitch_games: Dict) -> List[str]:
        """Sort game names by IGDB popularity (highest first)."""
        if not game_names:
            return game_names

        print(f"Sorting {len(game_names)} games by popularity")

        # First try to get IGDB IDs from Twitch drops
        game_ids = []
        found_by_id = []
        for game_name in game_names:
            found = False
            for game in twitch_games.keys():
                if game.name == game_name:
                    game_ids.append(game.id)
                    found_by_id.append(game_name)
                    found = True
                    break
            if not found:
                print(f"Game not found in Twitch drops: {game_name}")

        print(f"Found {len(game_ids)} games in Twitch drops, {len(game_names) - len(game_ids)} need name search")

        # Get IGDB data for games found in drops
        games_data = []
        if game_ids:
            # Pass the corresponding game names for better caching
            found_game_names = [name for name in found_by_id]
            games_data = await self.get_games_data(game_ids, found_game_names)
            print(f"Retrieved IGDB data for {len(games_data)} games from drops")

        # For games not found in drops, search by name
        games_not_found = [name for name in game_names if name not in found_by_id]
        if games_not_found:
            print(f"Searching IGDB by name for {len(games_not_found)} games")
            name_search_results = await self.search_games_by_names(games_not_found)
            games_data.extend(name_search_results)
            print(f"Found {len(name_search_results)} additional games by name search")

        if not games_data:
            print("No IGDB data found - keeping original order")
            return game_names

        # Get popularity data for all games
        all_game_ids = [game.igdb_id for game in games_data]
        popularity_data = await self.get_popularity_data(all_game_ids)

        # Create mapping of game names to IGDB data including popularity
        igdb_data = {}
        for game in games_data:
            popularity = popularity_data.get(game.igdb_id, 0)
            igdb_data[game.name] = {
                "release_date": game.release_date,
                "rating": game.rating,
                "popularity": popularity
            }

        print(f"Found IGDB data for {len(igdb_data)} games total")

        # Sort by popularity (highest first)
        def get_popularity(game_name):
            game_data = igdb_data.get(game_name, {})
            popularity = game_data.get("popularity", 0)
            return popularity

        return sorted(game_names, key=get_popularity, reverse=True)
