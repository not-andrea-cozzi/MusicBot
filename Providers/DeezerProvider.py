# Providers/DeezerProvider.py
import asyncio
import logging
import re
import time
import unicodedata
from typing import Optional

import httpx

# Providers/DeezerProvider.py — aggiungere in cima al file, accanto agli altri import/regex

_ALT_VERSION_RE = re.compile(
    r'\b(instrumental(?:s)?|karaoke|a\s*cappella(?:s)?|acapella(?:s)?|sped\s*up|'
    r'nightcore|slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
    re.IGNORECASE,
)

_VERSION_TAG_RE = re.compile(
    r'\b(?:remix|re-?mix|radio\s+edit|extended|vip|club\s+mix|'
    r'dub\s+mix|original\s+mix|acoustic|live|demo)\b',
    re.IGNORECASE,
)


def _is_alt_version(text: str) -> bool:
    return bool(text) and bool(_ALT_VERSION_RE.search(text))


def _has_version_tag(text: str) -> bool:
    return bool(text) and bool(_VERSION_TAG_RE.search(text))


def _title_similarity_simple(a: str, b: str) -> float:
    """Similarity locale senza dipendenza da TextCleaner (evita import circolare)."""
    if not a or not b:
        return 0.0
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return 1.0
    ta, tb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


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
        local_cache=None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self.log = logger or logging.getLogger(__name__)

        self.local_cache = local_cache
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
        if not album_id:
            return {}

        async with self._album_data_cache_lock:
            if album_id in self._album_data_cache:
                return self._album_data_cache[album_id]

        if self.local_cache:
            cached_meta = self.local_cache.get_deezer_album_meta(album_id)
            if cached_meta is not None:
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
            return {}
        try:
            return r.json()
        except Exception as exc:
            self.log.debug(f"[Deezer] JSON non valido track {track_id}: {exc}")
            return {}

    def _search(self, **fields: str) -> str:
        parts = []
        for field, value in fields.items():
            if value:
                clean = _clean_query_term(value)
                if clean:
                    parts.append(f'{field}:"{clean}"')
        return " ".join(parts)

    def _search_free(self, **fields: str) -> str:
        """Query senza quoting: fallback quando la quoted-query non trova nulla."""
        return " ".join(_clean_query_term(v) for v in fields.values() if v)

    async def _search_with_fallback(self, limit: int, **fields: str) -> list:
        query = self._search(**fields)
        data = await self._get(self.SEARCH_URL, {"q": query, "limit": limit}) if query else []
        if not data:
            free_query = self._search_free(**fields)
            if free_query and free_query != query:
                data = await self._get(self.SEARCH_URL, {"q": free_query, "limit": limit})
        return data

    async def get_by_isrc(self, isrc: str) -> dict:
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
            album_data = await self._get_album_data(album_id)
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

    async def get_cover_url(self, title: str, artist: str = "", album: str = "") -> Optional[str]:
        if not title and not artist:
            return None

        attempts = []
        if artist and album and title:
            attempts.append({"artist": artist, "album": album, "track": title})
        if artist and title:
            attempts.append({"artist": artist, "track": title})
        if artist and album:
            attempts.append({"artist": artist, "album": album})
        if artist:
            attempts.append({"artist": artist})

        for fields in attempts:
            tracks = await self._search_with_fallback(10, **fields)
            best = _pick_best_track(tracks, album)
            if best:
                cover = _best_cover(best.get("album", {}))
                if cover:
                    return cover
        return None

    async def get_genre(self, title: str, artist: str) -> str:
        if not title or not artist:
            return ""
        try:
            data = await self._search_with_fallback(5, track=title, artist=artist)
            if not data:
                return ""
            best = _pick_best_track(data, "")
            if not best:
                return ""
            album_id = best.get("album", {}).get("id")
            if not album_id:
                return ""
            album_data = await self._get_album_data(album_id)
            genres = album_data.get("genres", {}).get("data", [])
            if genres:
                return genres[0].get("name", "")
        except Exception as exc:
            self.log.warning(f"[Deezer] get_genre fallito: {exc}")
        return ""

    async def get_track_and_disc(self, title: str, artist: str, album: str = "") -> tuple[int, int]:
        if not title or not artist:
            return 0, 0
        try:
            data = await self._search_with_fallback(5, track=title, artist=artist, album=album)
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
        try:
            data = await self._search_with_fallback(5, track=title, artist=artist, album=album)
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
                album_data = await self._get_album_data(album_id)
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
        if not title or not artist:
            return {}
        data = await self._search_with_fallback(10, track=title, artist=artist)
        best = _pick_best_track(data, "")
        if not best:
            return {}

        track_id = best.get("id")
        if not track_id:
            return {}

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
    
    # Providers/DeezerProvider.py — aggiungere questi metodi dentro class DeezerProvider

    async def search_recording(
        self,
        title: str,
        artist: str = "",
        album_hint: str = "",
        duration_ms: Optional[int] = None,
        isrc: str = "",
        min_score: float = 0.5,
    ) -> dict:
        """
        Ricerca strutturata equivalente a MusicBrainzApiRequstor.find_best_recording.
        Priorità: ISRC diretto -> search testuale con filtro alt-version esplicito.

        Ritorna un dict "raw" compatibile con MetaMapper.from_deezer_isrc,
        con chiavi aggiuntive:
          - "_release_edition_kind": "single" | "ep" | "album" | "compilation" | "unknown"
          - "_alt_version_rejected": bool (True se scartati candidati per alt-version)
        """
        if not title:
            return {}

        title_has_remix = _has_version_tag(title)
        alt_rejected = False

        # 1. ISRC diretto
        if isrc:
            raw = await self.get_by_isrc(isrc)
            if raw:
                cand_title = raw.get("title", "")
                if _is_alt_version(cand_title) and not _is_alt_version(title):
                    self.log.debug(f"[Deezer] ISRC {isrc} -> alt-version scartata: {cand_title!r}")
                    alt_rejected = True
                elif title_has_remix and not _has_version_tag(cand_title):
                    self.log.debug(f"[Deezer] ISRC {isrc} -> tag versione incoerente: {cand_title!r}")
                else:
                    raw["_release_edition_kind"] = await self._edition_kind_for_track_raw(raw)
                    raw["_alt_version_rejected"] = False
                    return raw

        # 2. Search testuale
        data = await self._search_with_fallback(15, track=title, artist=artist, album=album_hint)
        if not data:
            return {}

        best, best_score = None, -1.0
        for t in data:
            t_title = t.get("title", "")

            if _is_alt_version(t_title) and not _is_alt_version(title):
                alt_rejected = True
                continue

            if title_has_remix and not _has_version_tag(t_title):
                continue

            title_sim = _title_similarity_simple(title, t_title)
            if title_sim < 0.55:
                continue

            t_artist = (t.get("artist") or {}).get("name", "")
            artist_sim = _title_similarity_simple(artist, t_artist) if artist else 1.0
            if artist and artist_sim < 0.5:
                continue

            score = 0.6 * title_sim + 0.4 * artist_sim
            cand_ms = t.get("duration")
            if duration_ms and cand_ms:
                delta = abs(duration_ms - int(cand_ms) * 1000)
                if delta <= 5000:
                    score += 0.05

            if score > best_score:
                best_score, best = score, t

        # Fallback: se il tag era obbligatorio e nessun candidato lo aveva,
        # riprova senza il vincolo di tag (ultima risorsa, come MB include_version_tag).
        if not best and title_has_remix:
            for t in data:
                t_title = t.get("title", "")
                if _is_alt_version(t_title) and not _is_alt_version(title):
                    continue
                title_sim = _title_similarity_simple(title, t_title)
                if title_sim < 0.55:
                    continue
                if title_sim > best_score:
                    best_score, best = title_sim, t

        if not best or best_score < min_score:
            return {}

        track_id = best.get("id")
        track_data = await self._get_track_data(track_id) if track_id else {}
        album = best.get("album", {})
        album_id = album.get("id")

        artist_collection = ", ".join(
            c.get("name", "") for c in track_data.get("contributors", []) if c.get("name")
        )

        result = {
            "title":             best.get("title", ""),
            "artist":            best.get("artist", {}).get("name", ""),
            "artist_collection": artist_collection,
            "album":             album.get("title", ""),
            "cover_url":         _best_cover(album),
            "duration_ms":       int(best.get("duration", 0)) * 1000,
            "isrc":              track_data.get("isrc", ""),
            "explicit":          bool(track_data.get("explicit_lyrics", False)),
            "_alt_version_rejected": alt_rejected,
        }

        if track_data:
            result["track_number"] = int(track_data.get("track_position", 0))
            result["disc_number"] = int(track_data.get("disk_number", 0))

        if album_id:
            album_data = await self._get_album_data(album_id)
            if album_data:
                genres = album_data.get("genres", {}).get("data", [])
                if genres:
                    result["genre"] = genres[0].get("name", "").title()
                rd = album_data.get("release_date", "")
                if rd and len(rd) >= 4:
                    result["year"] = rd[:4]
                result["_release_edition_kind"] = self._record_type_to_edition_kind(
                    album_data.get("record_type", ""), album_data.get("nb_tracks", 0)
                )

        return result

    async def _edition_kind_for_track_raw(self, raw: dict) -> str:
        """Risolve edition kind quando il match arriva da get_by_isrc (non ha album_id diretto in raw)."""
        album_title = raw.get("album", "")
        if not album_title:
            return "unknown"
        return "unknown"  # get_by_isrc non espone record_type; kind resta unknown, non blocca il guard

    @staticmethod
    def _record_type_to_edition_kind(record_type: str, track_count: int) -> str:
        rt = (record_type or "").lower()
        if rt == "single":
            return "single"
        if rt == "ep":
            return "ep"
        if rt == "album":
            return "album"
        if rt == "compile":
            return "compilation"
        if track_count and track_count <= 2:
            return "single"
        return "unknown"