from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from Database.Model.ItunesModel import AppleMusicTrack
from Database.Service.TrackService import TrackService
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ItunesProviderUrls:
    BASE: str = "https://itunes.apple.com"
    SEARCH: str = BASE + "/search"
    LOOKUP: str = BASE + "/lookup"


class ITunesHttpClient:
    """
    Incapsula tutto l'accesso HTTP a iTunes Search/Lookup API:
    throttle, retry con backoff, fallback paese, query primitives.
    Nessuna logica di scoring/matching qui.
    """

    ITUNES_LIMIT: int = 500
    _FALLBACK_COUNTRIES: List[str] = ["US", "IT", "ES"]

    def __init__(
        self,
        session: Optional[httpx.AsyncClient] = None,
        logger: Optional[logging.Logger] = None,
        min_request_interval: float = 1.0,
        country: str = "US",
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        self._client = session
        self._owns_client = session is None
        self.logger = logger or logging.getLogger(__name__)
        self.country = country.upper()
        self._min_request_interval = max(5.0, min_request_interval)
        self._last_request_time = 0.0
        self._request_lock = asyncio.Lock()
        self.db_session: Optional[AsyncSession] = db_session

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    def set_country(self, country: str) -> None:
        if country:
            self.country = country.upper()

    # ------------------------------------------------------------------
    # Throttle & retry
    # ------------------------------------------------------------------
    async def _throttle(self) -> None:
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.monotonic()

    async def _get_with_retry(self, url: str, params: Dict, max_retries: int = 3) -> List[Dict]:
        for attempt in range(max_retries):
            try:
                await self._throttle()
                response = await self.client.get(url, params=params)
                if response.status_code in (429, 403):
                    wait = 2 ** attempt + 1
                    self.logger.warning(
                        f"[iTunes] Rate limit ({response.status_code}), attendo {wait}s "
                        f"(tentativo {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                if response.status_code != 200:
                    self.logger.debug(f"[iTunes] HTTP {response.status_code} — {url} {params}")
                    return []
                try:
                    return response.json().get("results", [])
                except ValueError as exc:
                    self.logger.debug(f"[iTunes] JSON non valido: {exc}")
                    return []
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.logger.debug(f"[iTunes] Tentativo {attempt + 1} fallito: {exc}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        return []

    async def get(self, url: str, params: Dict, use_fallback_countries: bool = True) -> List[Dict]:
        countries = [self.country]
        if use_fallback_countries:
            countries += [c for c in self._FALLBACK_COUNTRIES if c != self.country]

        for country in countries:
            results = await self._get_with_retry(url, {**params, "country": country})
            if results:
                if country != self.country:
                    self.logger.debug(f"[iTunes] Usato paese fallback: {country}")
                return results
        return []

    # ------------------------------------------------------------------
    # Query primitives
    # ------------------------------------------------------------------
    async def search_songs(self, term: str, limit: int = ITUNES_LIMIT, attribute: str = "") -> List[Dict]:
        params = {
            "term": term, "media": "music", "entity": "song",
            "limit": limit, "version": 2, "explicit": "Yes",
        }
        if attribute:
            params["attribute"] = attribute
        return await self.get(ItunesProviderUrls.SEARCH, params)

    async def search_albums(self, term: str, limit: int = 10) -> List[Dict]:
        return await self.get(ItunesProviderUrls.SEARCH, {
            "term": term, "media": "music", "entity": "album",
            "attribute": "albumTerm", "limit": limit, "version": 2,
        })

    async def lookup_artist_songs(self, artist_id: int, limit: int = ITUNES_LIMIT, sort: str = "recent") -> List[Dict]:
        params: Dict[str, Any] = {"id": artist_id, "entity": "song", "limit": limit}
        if sort:
            params["sort"] = sort
        return await self.get(ItunesProviderUrls.LOOKUP, params)

    async def lookup_album_songs(self, collection_id: int) -> List[Dict]:
        """DB-first: se le tracce sono già in cache locale, evita la chiamata a iTunes."""
        self.logger.debug(f"[iTunes] Lookup tracce per collectionId={collection_id}")

        if self.db_session:
            db_tracks = await TrackService.get_by_album(self.db_session, collection_id)
            if db_tracks:
                self.logger.debug(f"[iTunes] Cache DB hit collectionId={collection_id} ({len(db_tracks)} tracce)")
                return [self._track_model_to_dict(t) for t in db_tracks]

        return await self.get(ItunesProviderUrls.LOOKUP, {"id": collection_id, "entity": "song"})

    @staticmethod
    def _track_model_to_dict(t: AppleMusicTrack) -> Dict:
        return {
            "wrapperType": "track", "kind": "song",
            "trackId": t.track_id, "artistId": t.artist_id, "collectionId": t.collection_id,
            "artistName": t.artist_name, "collectionName": t.collection_name, "trackName": t.track_name,
            "collectionArtistName": t.collection_artist_name, "artworkUrl100": t.artwork_url,
            "trackExplicitness": t.track_explicitness, "discCount": t.disc_count,
            "discNumber": t.disc_number, "trackCount": t.track_count, "trackNumber": t.track_number,
            "trackTimeMillis": t.track_time_millis, "primaryGenreName": t.primary_genre_name,
            "isrc": t.isrc,
        }

    async def lookup_artist_id(self, artist_name: str) -> Optional[int]:
        from Algorithm.TextCleaner import TextCleaner

        results = await self.get(ItunesProviderUrls.SEARCH, {
            "term": artist_name, "media": "music", "entity": "musicArtist",
            "attribute": "artistTerm", "limit": 5, "version": 2,
        })
        norm_target = TextCleaner.normalize(artist_name)
        for item in results:
            if item.get("wrapperType") == "artist" and TextCleaner.normalize(item.get("artistName", "")) == norm_target:
                return item.get("artistId")
        return next((item.get("artistId") for item in results if item.get("artistId")), None)