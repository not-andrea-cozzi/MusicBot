from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from Algorithm.BestMatch import TrackMatcher
from Algorithm.TextCleaner import TextCleaner
from Utils.MusicPatterns import MusicPatterns


class SpotifyProvider:

    REQUEST_DELAY = 3.0

    _EXCLUDE_TITLE_RE = re.compile(
        r'\b(instrumental|karaoke|a\s*cappella|acapella|sped\s*up|nightcore|'
        r'slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
        re.IGNORECASE,
    )

    # Stesso pattern usato altrove (MusicBrainzHelper, MusicPatterns) per
    # riconoscere tag di versione "legittimi" (remix/live/acoustic/ecc.).
    # Usato per il bonus esplicito in search_allow_version_tag e per capire
    # se il titolo cercato richiede un tag di versione nel candidato.
    _VERSION_TAG_RE = MusicPatterns.VERSION_TAG_RE

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        logger: Optional[logging.Logger] = None,
        matcher: Optional[TrackMatcher] = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.is_active = False
        self._last_call_ts: float = 0.0

        # Stesso TrackMatcher (o compatibile) usato da iTunes/MB/LocalDbMatcher:
        # un solo algoritmo di scoring per tutto il bot. Se il pipeline
        # inietta il proprio matcher condiviso (vedi MetadataPipeline.__init__
        # -> self.matcher), usa quello; altrimenti istanzia uno di default
        # con la stessa soglia minima usata altrove.
        self.matcher = matcher or TrackMatcher(min_score=MusicPatterns.MATCHER_MIN_SCORE)

        try:
            auth = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
            )
            self._sp = spotipy.Spotify(auth_manager=auth)
            self.is_active = True
            self.log.debug("[Spotify] Client inizializzato.")
        except Exception as exc:
            self.log.warning(f"[Spotify] Init fallita: {exc}")

    def set_matcher(self, matcher: TrackMatcher) -> None:
        """Permette al pipeline di iniettare il matcher condiviso (stesso pattern di MusicBrainzApiRequstor)."""
        self.matcher = matcher

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        wait = self.REQUEST_DELAY - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()

    # ------------------------------------------------------------------
    # Public search
    # ------------------------------------------------------------------
    def search(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        duration_ms: Optional[int] = None,
        isrc: str = "",
    ) -> Optional[dict]:
        if not self.is_active or not title:
            return None

        # 1. Lookup diretto per ISRC
        if isrc:
            result = self._search_by_isrc(isrc)
            if result:
                self.log.debug(f"[Spotify] Match ISRC: {isrc}")
                return result

        # 2. Ricerca per titolo + artista
        art_primary = TextCleaner.primary_artist(artist) if artist else ""
        query = f"track:{title}"
        if art_primary:
            query += f" artist:{art_primary}"
        if album:
            query += f" album:{album}"

        try:
            self._throttle()
            results = self._sp.search(q=query, type="track", limit=10)
            tracks = (results or {}).get("tracks", {}).get("items", [])
        except Exception as exc:
            self.log.debug(f"[Spotify] Search fallita: {exc}")
            return None

        if not tracks:
            # Fallback senza album
            try:
                q2 = f"track:{title}" + (f" artist:{art_primary}" if art_primary else "")
                self._throttle()
                results = self._sp.search(q=q2, type="track", limit=10)
                tracks = (results or {}).get("tracks", {}).get("items", [])
            except Exception:
                return None

        best = self._pick_best(tracks, title, artist, duration_ms, allow_tag=False)
        if best:
            self.log.debug(
                f"[Spotify] Trovato: '{best.get('name')}' "
                f"di '{best.get('artists', [{}])[0].get('name', '')}'"
            )
        return best

    def search_allow_version_tag(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        duration_ms: Optional[int] = None,
        isrc: str = "",
    ) -> Optional[dict]:
        """
        Retry esplicito per quando il titolo richiede un tag di versione
        (remix/live/acoustic/ecc.) ma `search()` non ha trovato un candidato
        coerente. Query senza `track:` quotato (termine libero + filtro
        artista), e selezione che RICHIEDE esplicitamente il tag di
        versione nel candidato (vedi `_pick_best` con allow_tag=True).
        """
        if not self.is_active or not title:
            return None

        art_primary = TextCleaner.primary_artist(artist) if artist else ""
        free_title = re.sub(r'[\(\[][^\)\]]*[\)\]]', '', title).strip() or title
        tag_match = re.search(r'[\(\[]([^\)\]]+)[\)\]]', title)
        tag = tag_match.group(1).strip() if tag_match else ""

        query = f"{free_title} {tag}".strip() if tag else free_title
        if art_primary:
            query = f"{query} artist:{art_primary}"

        try:
            self._throttle()
            results = self._sp.search(q=query, type="track", limit=10)
            tracks = (results or {}).get("tracks", {}).get("items", [])
        except Exception as exc:
            self.log.debug(f"[Spotify] search_allow_version_tag fallita: {exc}")
            return None

        if not tracks:
            return None

        best = self._pick_best(tracks, title, artist, duration_ms, allow_tag=True)
        if best:
            self.log.debug(
                f"[Spotify] (retry tag) Trovato: '{best.get('name')}' "
                f"di '{best.get('artists', [{}])[0].get('name', '')}'"
            )
        return best

    def _search_by_isrc(self, isrc: str) -> Optional[dict]:
        try:
            self._throttle()
            results = self._sp.search(q=f"isrc:{isrc}", type="track", limit=1)
            tracks = (results or {}).get("tracks", {}).get("items", [])
            return tracks[0] if tracks else None
        except Exception as exc:
            self.log.debug(f"[Spotify] ISRC search fallita: {exc}")
            return None

    # ------------------------------------------------------------------
    # Selezione candidato — unificata su TrackMatcher (Algorithm.BestMatch)
    # ------------------------------------------------------------------
    def _pick_best(self, tracks: list, title: str, artist: str, duration_ms: Optional[int], allow_tag: bool) -> Optional[dict]:
        art_primary = TextCleaner.primary_artist(artist) if artist else ""
        wants_tag = bool(self._VERSION_TAG_RE.search(title)) if allow_tag else False

        best, best_score = None, -1.0
        for t in tracks:
            t_name = t.get("name", "")

            if self._EXCLUDE_TITLE_RE.search(t_name) and not self._EXCLUDE_TITLE_RE.search(title):
                continue

            if allow_tag:
                has_tag = bool(self._VERSION_TAG_RE.search(t_name))
                if wants_tag and not has_tag:
                    continue  # scarta la versione "pulita": non è quella richiesta

            candidate = self.spotify_track_to_candidate(t)
            score = self.matcher.score_candidate(
                title=title, artist=art_primary, album_hint="",
                duration_ms=duration_ms, isrc="", candidate=candidate,
            )
            if score is None:
                continue

            if score > best_score:
                best_score, best = score, t

        return best

    @staticmethod
    def map_to_meta(track: dict) -> dict:
        """Converte un track Spotify nel formato dict compatibile con SongMeta."""
        if not track:
            return {}

        album_obj = track.get("album", {})
        artists = track.get("artists", [])
        album_artists = album_obj.get("artists", [])

        artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        all_artists = [a.get("name", "") for a in artists if a.get("name")]
        artist_ids = [a.get("id", "") for a in artists if a.get("id")]

        album_artist = (
            album_artists[0].get("name", "") if album_artists else
            artists[0].get("name", "") if artists else ""
        )
        all_album_artists = [a.get("name", "") for a in album_artists if a.get("name")]

        release_date = album_obj.get("release_date", "")
        year = release_date[:4] if release_date and len(release_date) >= 4 else ""

        images = album_obj.get("images", [])
        cover_url = images[0].get("url", "") if images else ""

        external_ids = track.get("external_ids", {})
        isrc = external_ids.get("isrc", "")
        artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        artist_collection = "; ".join(a.get("name", "") for a in artists if a.get("name"))

        return {
            "title":             track.get("name", ""),
            "artist":            artist_name,
            "artists_list":      all_artists,
            "artist_ids":        artist_ids,
            "album_artist":      album_artist,
            "album_artists_list": all_album_artists,
            "album":             album_obj.get("name", ""),
            "album_id":          album_obj.get("id", ""),
            "album_type":        album_obj.get("album_type", ""),
            "year":              year,
            "track_number":      track.get("track_number", 0),
            "disc_number":       track.get("disc_number", 0),
            "total_tracks":      album_obj.get("total_tracks", 0),
            "cover_url":         cover_url,
            "explicit":          bool(track.get("explicit", False)),
            "isrc":              isrc,
            "duration_ms":       track.get("duration_ms"),
            "artist_collection": artist_collection
        }
    
    def spotify_track_to_candidate(self, track: Dict[str, Any]) -> Dict[str, Any]:
        artists = track.get("artists", [])
        album_obj = track.get("album", {}) or {}
        external_ids = track.get("external_ids", {}) or {}
    
        return {
            "trackName": track.get("name", "") or "",
            "artistName": artists[0].get("name", "") if artists else "",
            "collectionName": album_obj.get("name", "") or "",
            "trackTimeMillis": track.get("duration_ms"),
            "isrc": external_ids.get("isrc", "") or "",
        }
    