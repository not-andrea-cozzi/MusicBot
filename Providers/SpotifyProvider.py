from __future__ import annotations

import logging
import re
import time
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from Algorithm.TextCleaner import TextCleaner


class SpotifyProvider:

    REQUEST_DELAY = 3.0

    _EXCLUDE_TITLE_RE = re.compile(
        r'\b(instrumental|karaoke|a\s*cappella|acapella|sped\s*up|nightcore|'
        r'slowed(?:\s*(?:and|&)?\s*reverb(?:ed)?)?|8d\s*audio|tiktok\s*remix)\b',
        re.IGNORECASE,
    )

    # NUOVO: stesso pattern usato altrove (MusicBrainzHelper, MusicPatterns)
    # per riconoscere tag di versione "legittimi" (remix/live/acoustic/ecc.),
    # usato da search_allow_version_tag per accettare esplicitamente
    # candidati con questo tag invece di scartarli.
    _VERSION_TAG_RE = re.compile(
        r'[\(\[]\s*(?:remix|re-?mix|radio\s+edit|extended|vip|club\s+mix|'
        r'dub\s+mix|original\s+mix|acoustic|live|demo|instrumental)\b',
        re.IGNORECASE,
    )

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.is_active = False
        self._last_call_ts: float = 0.0
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

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        wait = self.REQUEST_DELAY - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.monotonic()

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

        best = self._pick_best(tracks, title, artist, duration_ms)
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
        coerente. Differenze rispetto a `search()`:

        - Query senza il termine `track:` quotato sull'intero titolo (che
          su Spotify può comportarsi come ricerca esatta e scartare la
          traccia se il titolo indicizzato differisce anche di poco, es.
          per un featuring incluso solo nel campo artisti): usa invece il
          titolo come termine libero + filtro artista.
        - `_pick_best_allow_tag` non scarta i candidati che hanno un tag di
          versione nel nome (a differenza di `_pick_best`, che comunque non
          li escludeva esplicitamente se non in _EXCLUDE_TITLE_RE — questa
          variante aggiunge un bonus di score ai candidati che hanno
          ESPLICITAMENTE lo stesso tag richiesto, per preferirli a parità
          di altri fattori).

        Non sostituisce `search()`: va chiamato solo come secondo tentativo
        quando il primo giro fallisce o produce un match senza il tag.
        """
        if not self.is_active or not title:
            return None

        art_primary = TextCleaner.primary_artist(artist) if artist else ""
        # Termine libero (non quotato con track:) per massimizzare il recall
        # quando il titolo ha un tag tra parentesi che potrebbe non essere
        # indicizzato identicamente su Spotify.
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

        best = self._pick_best_allow_tag(tracks, title, artist, duration_ms)
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

    def _pick_best(self, tracks, title, artist, duration_ms):
        title_norm  = TextCleaner.normalize(title)
        artist_norm = TextCleaner.normalize(TextCleaner.primary_artist(artist)) if artist else ""
        best, best_score = None, -1.0

        for t in tracks:
            t_name = t.get("name", "")
            if self._EXCLUDE_TITLE_RE.search(t_name) and not self._EXCLUDE_TITLE_RE.search(title):
                continue

            t_norm = TextCleaner.normalize(t_name)
            title_sim = TextCleaner.title_similarity(title_norm, t_norm)
            if title_sim < 0.75:
                continue

            artists = t.get("artists", [])
            a_norm  = TextCleaner.normalize(artists[0].get("name", "")) if artists else ""
            art_sim = TextCleaner.title_similarity(artist_norm, a_norm) if artist_norm else 1.0
            if art_sim < 0.70:
                continue

            score = 0.6 * title_sim + 0.4 * art_sim
            cand_ms = t.get("duration_ms")
            if duration_ms and cand_ms and abs(duration_ms - cand_ms) <= 5000:
                score += 0.05

            if score > best_score:
                best_score, best = score, t

        return best

    def _pick_best_allow_tag(self, tracks, title, artist, duration_ms):
        """
        Variante di _pick_best usata da search_allow_version_tag: richiede
        ESPLICITAMENTE che il candidato abbia lo stesso tag di versione del
        titolo cercato (remix/live/acoustic/ecc.), scartando i candidati che
        ne sono privi — l'inverso esatto del problema originale, dove la
        versione "pulita" (originale) vinceva per somiglianza testuale pur
        non essendo quella richiesta.
        """
        title_norm  = TextCleaner.normalize(title)
        artist_norm = TextCleaner.normalize(TextCleaner.primary_artist(artist)) if artist else ""
        wants_tag   = bool(self._VERSION_TAG_RE.search(title))

        best, best_score = None, -1.0

        for t in tracks:
            t_name = t.get("name", "")

            if self._EXCLUDE_TITLE_RE.search(t_name) and not self._EXCLUDE_TITLE_RE.search(title):
                continue

            has_tag = bool(self._VERSION_TAG_RE.search(t_name))
            if wants_tag and not has_tag:
                continue  # scarta la versione "pulita": non è quella richiesta

            t_norm = TextCleaner.normalize(t_name)
            title_sim = TextCleaner.title_similarity(title_norm, t_norm)
            if title_sim < 0.60:
                # soglia più permissiva di _pick_best: un titolo con tag
                # versione e featuring può discostarsi più del solito dal
                # titolo "seed" (es. "Leaked (Remix)" vs "Leaked (feat. X) [Remix]")
                continue

            artists = t.get("artists", [])
            a_norm  = TextCleaner.normalize(artists[0].get("name", "")) if artists else ""
            art_sim = TextCleaner.title_similarity(artist_norm, a_norm) if artist_norm else 1.0
            if art_sim < 0.70:
                continue

            score = 0.6 * title_sim + 0.4 * art_sim
            cand_ms = t.get("duration_ms")
            if duration_ms and cand_ms and abs(duration_ms - cand_ms) <= 5000:
                score += 0.05

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