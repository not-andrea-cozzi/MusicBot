from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx


class DeezerHttpClient:
    """
    Incapsula tutto l'accesso HTTP a Deezer: throttle, retry su 429,
    cache in-memory per dati album/track.
    Nessuna logica di matching/scoring qui.
    """

    SEARCH_URL = "https://api.deezer.com/search"
    SEARCH_ALBUM_URL = "https://api.deezer.com/search/album"
    SEARCH_ISRC: str = "https://api.deezer.com/2.0/track/isrc:{isrc}"

    _MIN_INTERVAL = 0.25

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self.log = logger or logging.getLogger(__name__)
        self._album_data_cache: dict[int, dict] = {}
        self._album_data_cache_lock = asyncio.Lock()


    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_INTERVAL:
            await asyncio.sleep(self._MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------
    # Generic GET with retry
    # ------------------------------------------------------------------
    async def get_list(self, url: str, params: dict, *, _retry: bool = True) -> list:
        async with self._lock:
            return await self._fetch_raw(url, params, _retry=_retry)

    async def _fetch_raw(self, url: str, params: dict, *, _retry: bool = True) -> list:
        await self._throttle()
        try:
            r = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            self.log.warning(f"[Deezer] Timeout su {url}: {exc}")
            return []
        except Exception as exc:
            self.log.debug(f"[Deezer] Errore rete su {url}: {exc}")
            return []

        if r.status_code == 429:
            if _retry:
                self.log.warning("[Deezer] Rate limit 429, attendo 5s e riprovo")
                await asyncio.sleep(5.0)
                await self._throttle()
                try:
                    r = await self._client.get(url, params=params)
                except Exception as exc:
                    self.log.debug(f"[Deezer] Errore retry su {url}: {exc}")
                    return []
            else:
                return []

        if r.status_code != 200:
            self.log.debug(f"[Deezer] HTTP {r.status_code} su {url}")
            return []

        try:
            return r.json().get("data", [])
        except Exception as exc:
            self.log.debug(f"[Deezer] JSON non valido: {exc}")
            return []

    async def get_object(self, url: str) -> dict:
        """GET che ritorna l'intero JSON (non paginato in 'data'), per /album/{id} e /track/{id}."""
        async with self._lock:
            await self._throttle()
            try:
                r = await self._client.get(url)
            except httpx.TimeoutException as exc:
                self.log.warning(f"[Deezer] Timeout su {url}: {exc}")
                return {}
            except Exception as exc:
                self.log.debug(f"[Deezer] Errore rete su {url}: {exc}")
                return {}

        if r.status_code != 200:
            return {}
        try:
            return r.json()
        except Exception as exc:
            self.log.debug(f"[Deezer] JSON non valido {url}: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Album data (cached in-memory) — include 'upc' nella risposta grezza Deezer
    # ------------------------------------------------------------------
    async def get_album_data(self, album_id: int) -> dict:
        if not album_id:
            return {}

        async with self._album_data_cache_lock:
            if album_id in self._album_data_cache:
                return self._album_data_cache[album_id]

        album_data = await self.get_object(f"https://api.deezer.com/album/{album_id}")
        if not album_data:
            return {}

        async with self._album_data_cache_lock:
            self._album_data_cache[album_id] = album_data

        return album_data

    async def get_track_data(self, track_id: int) -> dict:
        return await self.get_object(f"https://api.deezer.com/track/{track_id}")

    # ------------------------------------------------------------------
    # Search helpers
    # ------------------------------------------------------------------
    @staticmethod
    def build_query(quoted: bool, **fields: str) -> str:
        from Providers.Deezer.DeezerTextUtils import clean_query_term
        if quoted:
            parts = []
            for field, value in fields.items():
                if value:
                    clean = clean_query_term(value)
                    if clean:
                        parts.append(f'{field}:"{clean}"')
            return " ".join(parts)
        return " ".join(clean_query_term(v) for v in fields.values() if v)

    async def search_with_fallback(self, limit: int, **fields: str) -> list:
        query = self.build_query(True, **fields)
        data = await self.get_list(self.SEARCH_URL, {"q": query, "limit": limit}) if query else []
        if not data:
            free_query = self.build_query(False, **fields)
            if free_query and free_query != query:
                data = await self.get_list(self.SEARCH_URL, {"q": free_query, "limit": limit})
        return data

    async def get_isrc(self, isrc: str) -> dict:
        url = self.SEARCH_ISRC.format(isrc=isrc)
        return await self.get_object(url)