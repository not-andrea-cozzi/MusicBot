import asyncio
import logging
import re
import time
import unicodedata
from typing import Optional

import httpx


def _clean_query_term(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _album_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _best_cover(album_obj: dict) -> Optional[str]:
    for field in ("cover_xl", "cover_big", "cover_medium", "cover"):
        url = album_obj.get(field, "")
        if url:
            return url
    return None


_DELUXE_RE = re.compile(
    r'\b(?:deluxe|expanded|super\s+deluxe|anniversary|remastered|'
    r'special\s+edition|bonus\s+track)\b',
    re.IGNORECASE,
)


def _pick_best_track(tracks: list, hint_album: str) -> Optional[dict]:
    if not tracks:
        return None
    scored = []
    hint_is_deluxe = bool(_DELUXE_RE.search(hint_album)) if hint_album else False
    hint_norm = hint_album.lower().strip() if hint_album else ""

    for t in tracks:
        album = t.get("album", {})
        cover = _best_cover(album)
        if not cover:
            continue
        album_title = album.get("title", "")
        album_norm = album_title.lower().strip()
        sim = _album_similarity(hint_album, album_title) if hint_album else 0.5
        is_deluxe = bool(_DELUXE_RE.search(album_title))

        if hint_norm and album_norm == hint_norm:
            sim = min(1.0, sim + 0.20)
        elif is_deluxe and not hint_is_deluxe:
            sim -= 0.30
        elif hint_is_deluxe and not is_deluxe:
            sim -= 0.10

        scored.append((sim, t))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


class DeezerProvider:
    SEARCH_URL = "https://api.deezer.com/search"
    SEARCH_ALBUM_URL = "https://api.deezer.com/search/album"
    SEARCH_ISRC: str = "https://api.deezer.com/2.0/track/isrc:{isrc}"

    _MIN_INTERVAL = 0.25

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
        local_cache=None,  # NUOVO: cache album_id condivisa (es. LocalAlbumCache)
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self.log = logger or logging.getLogger(__name__)

        self.local_cache = local_cache
        # Cache in-memory per album_id → dati genere/anno (per-processo, vita breve)
        self._album_data_cache: dict[int, dict] = {}
        self._album_data_cache_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_INTERVAL:
            await asyncio.sleep(self._MIN_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    async def _get(self, url: str, params: dict, *, _retry: bool = True) -> list:
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

    async def _get_album_data(self, album_id: int) -> dict:
        """
        Fetch /album/{id} con cache a due livelli:
        1. In-memory (self._album_data_cache) — vita del processo, evita
           duplicati nello stesso run quando più tracce condividono l'album.
        2. local_cache (opzionale, persistente su disco) — solo genere/anno,
           per evitare round-trip HTTP anche tra run diversi.
        """
        if not album_id:
            return {}

        async with self._album_data_cache_lock:
            if album_id in self._album_data_cache:
                self.log.debug(f"[Deezer] [Cache-mem] hit album {album_id}")
                return self._album_data_cache[album_id]

        if self.local_cache:
            cached_meta = self.local_cache.get_deezer_album_meta(album_id)
            if cached_meta is not None:
                self.log.debug(f"[Deezer] [Cache-disk] hit album {album_id}")
                async with self._album_data_cache_lock:
                    self._album_data_cache[album_id] = cached_meta
                return cached_meta

        url = f"https://api.deezer.com/album/{album_id}"
        async with self._lock:
            await self._throttle()
            try:
                r = await self._client.get(url)
            except httpx.TimeoutException as exc:
                self.log.warning(f"[Deezer] Timeout album {album_id}: {exc}")
                return {}
            except Exception as exc:
                self.log.debug(f"[Deezer] Errore rete album {album_id}: {exc}")
                return {}

        if r.status_code != 200:
            self.log.debug(f"[Deezer] HTTP {r.status_code} per album {album_id}")
            return {}

        try:
            album_data = r.json()
        except Exception as exc:
            self.log.debug(f"[Deezer] JSON non valido album {album_id}: {exc}")
            return {}

        async with self._album_data_cache_lock:
            self._album_data_cache[album_id] = album_data

        if self.local_cache:
            self.local_cache.set_deezer_album_meta(album_id, album_data)
            self.log.debug(f"[Deezer] [Cache-disk] scritto album {album_id}")

        return album_data

    async def _get_track_data(self, track_id: int) -> dict:
        url = f"https://api.deezer.com/track/{track_id}"
        async with self._lock:
            await self._throttle()
            try:
                r = await self._client.get(url)
            except httpx.TimeoutException as exc:
                self.log.warning(f"[Deezer] Timeout track {track_id}: {exc}")
                return {}
            except Exception as exc:
                self.log.debug(f"[Deezer] Errore rete track {track_id}: {exc}")
                return {}

        if r.status_code != 200:
            self.log.debug(f"[Deezer] HTTP {r.status_code} per track {track_id}")
            return {}
        try:
            return r.json()
        except Exception as exc:
            self.log.debug(f"[Deezer] JSON non valido track {track_id}: {exc}")
            return {}

    # ── Query builders ────────────────────────────────────────────────────────

    def _search(self, **fields: str) -> str:
        parts = []
        for field, value in fields.items():
            if value:
                clean = _clean_query_term(value)
                if clean:
                    parts.append(f'{field}:"{clean}"')
        return " ".join(parts)

    # ── ISRC lookup (NUOVO) ──────────────────────────────────────────────────

    async def get_by_isrc(self, isrc: str) -> dict:
        """Lookup diretto: GET /2.0/track/isrc:{isrc}. Genere via /album/{id} (cached)."""
        if not isrc:
            return {}

        url = self.SEARCH_ISRC.format(isrc=isrc)
        async with self._lock:
            await self._throttle()
            try:
                r = await self._client.get(url)
            except Exception as exc:
                self.log.debug(f"[Deezer] ISRC fetch fallita {isrc}: {exc}")
                return {}

        if r.status_code != 200:
            return {}
        try:
            data = r.json()
        except Exception:
            return {}

        if data.get("error") or not data.get("id"):
            self.log.debug(f"[Deezer] ISRC {isrc} non trovato")
            return {}

        album = data.get("album", {})
        album_id = album.get("id")

        artist_collection = ", ".join(
            c.get("name", "") for c in data.get("contributors", []) if c.get("name")
        )

        result = {
            "title":             data.get("title", ""),
            "artist":            data.get("artist", {}).get("name", ""),
            "artist_collection": artist_collection,
            "album":             album.get("title", ""),
            "cover_url":         _best_cover(album),
            "duration_ms":       int(data.get("duration", 0)) * 1000,
            "track_number":      int(data.get("track_position", 0)),
            "disc_number":       int(data.get("disk_number", 0)),
            "isrc":              data.get("isrc", isrc),
            "explicit":          bool(data.get("explicit_lyrics", False)),
            "year":              (data.get("release_date") or "")[:4],
        }

        if album_id:
            album_data = await self._get_album_data(album_id)  # cached
            if album_data:
                genres = album_data.get("genres", {}).get("data", [])
                if genres:
                    result["genre"] = genres[0].get("name", "").title()
                if not result.get("year"):
                    rd = album_data.get("release_date", "")
                    if rd and len(rd) >= 4:
                        result["year"] = rd[:4]

        self.log.debug(f"[Deezer] ISRC {isrc} -> '{result['title']}' genre={result.get('genre')!r}")
        return result

    # ── Public API esistente (invariata) ─────────────────────────────────────

    async def get_cover_url(self, title: str, artist: str = "", album: str = "") -> Optional[str]:
        if not title and not artist:
            return None
        strategies = []
        if artist and album and title:
            strategies.append(self._search(artist=artist, album=album, track=title))
        if artist and title:
            strategies.append(self._search(artist=artist, track=title))
        if artist and album:
            strategies.append(self._search(artist=artist, album=album))
        if artist:
            strategies.append(self._search(artist=artist))

        for query in strategies:
            if not query:
                continue
            tracks = await self._get(self.SEARCH_URL, {"q": query, "limit": 10})
            best = _pick_best_track(tracks, album)
            if best:
                cover = _best_cover(best.get("album", {}))
                if cover:
                    return cover
        return None

    async def get_genre(self, title: str, artist: str) -> str:
        if not title or not artist:
            return ""
        query = self._search(track=title, artist=artist)
        if not query:
            return ""
        try:
            data = await self._get(self.SEARCH_URL, {"q": query, "limit": 5})
            if not data:
                return ""
            best = _pick_best_track(data, "")
            if not best:
                return ""
            album_id = best.get("album", {}).get("id")
            if not album_id:
                return ""
            album_data = await self._get_album_data(album_id)  # cached
            genres = album_data.get("genres", {}).get("data", [])
            if genres:
                return genres[0].get("name", "")
        except Exception as exc:
            self.log.warning(f"[Deezer] get_genre fallito: {exc}")
        return ""

    async def get_track_and_disc(self, title: str, artist: str, album: str = "") -> tuple[int, int]:
        if not title or not artist:
            return 0, 0
        query = self._search(track=title, artist=artist, album=album)
        if not query:
            return 0, 0
        try:
            data = await self._get(self.SEARCH_URL, {"q": query, "limit": 5})
            if not data:
                return 0, 0
            best = _pick_best_track(data, album)
            if not best:
                return 0, 0
            track_id = best.get("id")
            if not track_id:
                return 0, 0
            track_data = await self._get_track_data(track_id)
            return int(track_data.get("track_position", 0)), int(track_data.get("disk_number", 0))
        except Exception as exc:
            self.log.warning(f"[Deezer] get_track_and_disc fallito: {exc}")
        return 0, 0

    async def get_full_metadata(self, title: str, artist: str, album: str = "") -> dict:
        if not title and not artist:
            return {}
        query = self._search(track=title, artist=artist, album=album)
        if not query:
            return {}
        try:
            data = await self._get(self.SEARCH_URL, {"q": query, "limit": 5})
            if not data:
                return {}
            best = _pick_best_track(data, album)
            if not best:
                return {}

            result = {
                "title":       best.get("title", ""),
                "artist":      best.get("artist", {}).get("name", ""),
                "album":       best.get("album", {}).get("title", ""),
                "cover_url":   _best_cover(best.get("album", {})),
                "duration_ms": int(best.get("duration", 0)) * 1000,
            }

            track_id = best.get("id")
            album_id = best.get("album", {}).get("id")

            if track_id:
                track_data = await self._get_track_data(track_id)
                if track_data:
                    result["track_number"] = int(track_data.get("track_position", 0))
                    result["disc_number"] = int(track_data.get("disk_number", 0))
                    result["isrc"] = track_data.get("isrc", "")

            if album_id:
                album_data = await self._get_album_data(album_id)  # cached
                if album_data:
                    genres = album_data.get("genres", {}).get("data", [])
                    if genres:
                        result["genre"] = genres[0].get("name", "").title()
                    release_date = album_data.get("release_date", "")
                    if release_date and len(release_date) >= 4:
                        result["year"] = release_date[:4]

            return result
        except Exception as exc:
            self.log.warning(f"[Deezer] get_full_metadata fallito: {exc}")
        return {}
    
    async def search_by_title_artist(self, title: str, artist: str) -> dict:
        """Cerca titolo+artista su Deezer, restituisce metadati + ISRC."""
        if not title or not artist:
            return {}
        query = self._search(track=title, artist=artist)
        if not query:
            return {}
        data = await self._get(self.SEARCH_URL, {"q": query, "limit": 10})
        best = _pick_best_track(data, "")
        if not best:
            return {}

        track_id = best.get("id")
        if not track_id:
            return {}

        # Fetch dettaglio traccia per ISRC
        track_data = await self._get_track_data(track_id)
        if not track_data or not track_data.get("isrc"):
            return {}

        album = best.get("album", {})
        album_id = album.get("id")
        genre = ""
        year = ""
        if album_id:
            album_data = await self._get_album_data(album_id)
            genres = album_data.get("genres", {}).get("data", [])
            if genres:
                genre = genres[0].get("name", "").title()
            year = (album_data.get("release_date") or "")[:4]

        artist_collection = ", ".join(
            c.get("name", "") for c in track_data.get("contributors", []) if c.get("name")
        )

        return {
            "isrc":              track_data.get("isrc", ""),
            "title":             best.get("title", ""),
            "artist":            best.get("artist", {}).get("name", ""),
            "artist_collection": artist_collection,
            "album":             album.get("title", ""),
            "cover_url":         _best_cover(album),
            "duration_ms":       int(best.get("duration", 0)) * 1000,
            "track_number":      int(track_data.get("track_position", 0)),
            "disc_number":       int(track_data.get("disk_number", 0)),
            "explicit":          bool(track_data.get("explicit_lyrics", False)),
            "year":              year,
            "genre":             genre,
        }