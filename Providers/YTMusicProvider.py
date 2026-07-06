import threading
import logging
from typing import Optional, Tuple

from Algorithm.BestMatch import similarity
from Algorithm.TextCleaner import TextCleaner
from Model.SongMeta import SongMeta

try:
    from ytmusicapi import YTMusic
    _YTMUSIC_OK = True
except ImportError:
    _YTMUSIC_OK = False


_TRACK_MATCH_MIN_SIM = 0.82


def _normalize_thumb_url(url: str) -> str:
    """Porta un thumbnail Google a risoluzione massima."""
    if url and "=" in url and ("ggpht.com" in url or "googleusercontent.com" in url):
        return url.split("=")[0] + "=w3000-h3000"
    return url


class YTMusicProvider:
    """Provider per la ricerca e l'estrazione dei metadati da YouTube Music."""

    def __init__(self, accept_score: float, fallback_score: float, logger: logging.Logger):
        self.logger = logger
        self.accept_score = accept_score
        self.fallback_score = fallback_score
        self.client = None
        self.is_active = False

        self._init_lock = threading.Lock()

        # Cache album: browse_id → dict (o {} se fetch fallito)
        # _album_inflight: browse_id → threading.Event, usato per evitare
        # fetch duplicati concorrenti per lo stesso ID (fix race condition).
        self._album_cache: dict[str, dict] = {}
        self._album_inflight: dict[str, threading.Event] = {}
        self._cache_lock = threading.Lock()

        if _YTMUSIC_OK:
            self._initialize_client()
        else:
            self.logger.warning("[YTMusic] Modulo ytmusicapi non installato. Provider disabilitato.")

    # ------------------------------------------------------------------
    # Inizializzazione
    # ------------------------------------------------------------------

    def _initialize_client(self) -> None:
        with self._init_lock:
            try:
                self.client = YTMusic()
                self.is_active = True
                self.logger.debug("[YTMusic] Client inizializzato (modalità guest).")
            except Exception as e:
                self.logger.warning(f"[YTMusic] Inizializzazione fallita: {e}")

    # ------------------------------------------------------------------
    # Cache album — thread-safe senza race condition
    # ------------------------------------------------------------------

    def _get_album(self, browse_id: str) -> dict:
        """
        Recupera i dati di un album con cache thread-safe.

        Usa un threading.Event per evitare fetch duplicati concorrenti:
        il primo thread che richiede un browse_id lo scarica; tutti gli altri
        aspettano sul medesimo Event invece di lanciare richieste parallele.
        """
        with self._cache_lock:
            if browse_id in self._album_cache:
                return self._album_cache[browse_id]
            if browse_id in self._album_inflight:
                event = self._album_inflight[browse_id]
            else:
                event = threading.Event()
                self._album_inflight[browse_id] = event
                event = None

        if event is not None:
            event.wait(timeout=15)
            with self._cache_lock:
                return self._album_cache.get(browse_id, {})

        try:
            data = self.client.get_album(browse_id) or {}
        except Exception as e:
            self.logger.debug(f"[YTMusic] get_album fallito ({browse_id}): {e}")
            data = {}

        with self._cache_lock:
            self._album_cache[browse_id] = data
            ev = self._album_inflight.pop(browse_id, None)

        if ev is not None:
            ev.set()

        return data

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    def get_by_video_id(self, video_id: str) -> Optional[SongMeta]:
        """Recupera i metadati esatti di un brano dato il suo videoId."""
        if not self.is_active or not video_id:
            return None

        try:
            song = self.client.get_song(video_id)
        except Exception as e:
            self.logger.debug(f"[YTMusic] get_song({video_id}) fallito: {e}")
            return None

        details = (song or {}).get("videoDetails", {})
        if not details:
            return None

        raw_title  = details.get("title", "")
        raw_artist = details.get("author", "")

        isrc = (
            details.get("isrc", "")
            or (song.get("playerOverlays", {})
                .get("playerOverlayRenderer", {})
                .get("isrc", ""))
            or ""
        )

        mf = (song.get("microformat") or {}).get("microformatDataRenderer", {})
        year_str = (mf.get("publishDate", "") or "")[:4]

        thumbs = details.get("thumbnail", {}).get("thumbnails", [])
        cover_url = _normalize_thumb_url(thumbs[-1].get("url", "")) if thumbs else ""

        album_name   = ""
        track_number = ""

        try:
            try:
                wp = self.client.get_watch_playlist(videoId=video_id, limit=1)
            except TypeError:
                wp = self.client.get_watch_playlist(video_id, limit=1)

            tracks = (wp or {}).get("tracks", [])
            if tracks:
                t0        = tracks[0]
                album_obj = t0.get("album") or {}
                album_name = album_obj.get("name", "")
                album_id   = album_obj.get("id", "")

                if album_id:
                    album_data = self._get_album(album_id)
                    if album_data:
                        if not year_str:
                            year_str = str(album_data.get("year") or "")
                        al_thumbs = album_data.get("thumbnails") or []
                        if al_thumbs:
                            cover_url = _normalize_thumb_url(al_thumbs[-1]["url"])
                        track_number = self._extract_track_number(album_data, raw_title)
        except Exception as e:
            self.logger.debug(f"[YTMusic] get_watch_playlist({video_id}) fallito: {e}")

        # Popolamento oggetto SongMeta
        meta = SongMeta()
        meta.video_id = video_id
        meta.title = raw_title
        meta.artist = raw_artist
        meta.album = album_name
        meta.year = year_str[:4] if year_str else ""
        meta.cover_url = cover_url
        meta.isrc = isrc
        meta._ytmusic_score = 1.0

        if track_number and str(track_number).isdigit():
            meta.track_number = int(track_number)

        length_sec = details.get("lengthSeconds")
        if length_sec and str(length_sec).isdigit():
            meta.duration_ms = int(length_sec) * 1000

        self.logger.debug(
            f"[YTMusic] Trovato da VideoID: '{meta.title}' di '{meta.artist}'"
            + (f" ISRC={meta.isrc}" if meta.isrc else "")
        )
        return meta

    def search(
        self,
        title: str,
        artist: str = "",
        hint_album: str = "",
        strict_artist: bool = True,
    ) -> Optional[SongMeta]:
        """Cerca un brano e restituisce i metadati arricchiti valutando uno score."""
        if not self.is_active or not title.strip():
            return None

        art       = TextCleaner.primary_artist(artist) if artist and strict_artist else ""
        art_lower = art.lower() if art else ""
        query     = f"{art} {title}".strip() if art else title
        title_norm      = TextCleaner.normalize(title)
        album_hint_norm = TextCleaner.normalize(hint_album) if hint_album else ""

        try:
            results = self.client.search(query, filter="songs", limit=10)
        except Exception as e:
            self.logger.debug(f"[YTMusic] Eccezione nella ricerca: {e}")
            return None

        if not results:
            return None

        with self._cache_lock:
            known_album_ids = set(self._album_cache.keys())

        best, best_score = None, -1.0
        for item in results:
            s = self._score_item(item, title_norm, art_lower, known_album_ids, strict_artist)
            if s < self.fallback_score:
                continue

            if album_hint_norm:
                item_album = TextCleaner.normalize((item.get("album") or {}).get("name", ""))
                if item_album:
                    album_sim = TextCleaner.album_edition_similarity(album_hint_norm, item_album)
                    if album_sim >= 0.85:
                        s = min(1.0, s + 0.15)
                    elif album_sim < 0.50:
                        s = max(0.0, s - 0.15)

            if s > best_score:
                best_score = s
                best = item

        if not best or best_score < self.fallback_score:
            self.logger.debug(f"[YTMusic] Nessun candidato accettabile per: '{title}'")
            return None

        return self._build_search_result(best, title, artist, title_norm, best_score)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_item(
        self,
        item: dict,
        title_norm: str,
        art_lower: str,
        known_album_ids: set,
        strict_artist: bool = True,
    ) -> float:
        item_artists     = item.get("artists") or [{}]
        item_artist_lower = item_artists[0].get("name", "").lower()

        if strict_artist and art_lower:
            is_exact   = (item_artist_lower == art_lower)
            is_leading = item_artist_lower.startswith(art_lower + " ")
            if not (is_exact or is_leading):
                return -1.0

        raw_title = item.get("title", "")

        if TextCleaner.has_version_tag(raw_title) != TextCleaner.has_version_tag(title_norm):
            return -1.0

        item_norm  = TextCleaner.normalize(raw_title)
        title_sim  = similarity(title_norm, item_norm)
        score      = title_sim

        if score < self.fallback_score:
            return -1.0

        if art_lower and item_artist_lower == art_lower:
            score = min(1.0, score + 0.05)

        album_id   = (item.get("album") or {}).get("id", "")
        album_name = (item.get("album") or {}).get("name", "")

        if album_id and album_id in known_album_ids:
            score = min(1.0, score + 0.05)

        if album_id:
            album_data = self._get_album(album_id)
            n = self._track_count(album_data)
            album_norm = TextCleaner.normalize(album_name) if album_name else ""
            is_likely_single = (
                n <= 2
                or (album_norm and album_norm == item_norm)
            )
            if n > 6:
                score = min(1.0, score + 0.10)
            elif n > 3:
                score = min(1.0, score + 0.05)
            elif is_likely_single:
                score = max(0.0, score - 0.15)

        return score

    # ------------------------------------------------------------------
    # Costruzione risultato
    # ------------------------------------------------------------------

    def _build_search_result(
        self,
        best: dict,
        default_title: str,
        default_artist: str,
        title_norm: str,
        best_score: float,
    ) -> SongMeta:
        album_obj  = best.get("album") or {}
        album_id   = album_obj.get("id", "")
        album_name = album_obj.get("name", "")
        year_str   = str(best.get("year") or "")

        if album_name and TextCleaner.normalize(album_name) == title_norm:
            best_cached_id, best_cached_data, _ = self._find_album_for_single(title_norm)
            if best_cached_id:
                album_id   = best_cached_id
                album_name = best_cached_data.get("title", album_name)
                year_str   = str(best_cached_data.get("year") or year_str)
                album_obj  = {"id": album_id, "name": album_name}

        album_data = self._get_album(album_id) if album_id else {}
        if not year_str and album_data:
            year_str = str(album_data.get("year") or "")

        cover_url    = self._extract_cover_url(album_data, best)
        track_number = self._extract_track_number(album_data, best.get("title", default_title)) if album_data else ""

        real_artist = ", ".join(a["name"] for a in (best.get("artists") or []))
        if not real_artist:
            real_artist = default_artist

        # Popolamento oggetto SongMeta
        meta = SongMeta()
        meta.title = best.get("title", default_title)
        meta.artist = real_artist
        meta.album = album_obj.get("name", "")
        meta.year = year_str[:4] if year_str else ""
        meta.cover_url = cover_url
        meta._ytmusic_score = best_score
        
        if best.get("videoId"):
            meta.video_id = best["videoId"]

        if best.get("isExplicit") is not None:
            meta.explicit = bool(best["isExplicit"])

        if track_number and str(track_number).isdigit():
            meta.track_number = int(track_number)

        dur_sec = best.get("duration_seconds")
        if dur_sec:
            meta.duration_ms = int(dur_sec) * 1000

        return meta

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_album_for_single(self, title_norm: str) -> Tuple[Optional[str], Optional[dict], int]:
        with self._cache_lock:
            cached_albums = dict(self._album_cache)

        best_id, best_data, best_n = None, None, 0
        for cid, cdata in cached_albums.items():
            if not cdata:
                continue
            tracks_raw = cdata.get("tracks") or []
            track_list = tracks_raw if isinstance(tracks_raw, list) else (tracks_raw.get("items") or [])
            for t in track_list:
                if TextCleaner.normalize(t.get("title") or "") == title_norm:
                    n = len(track_list)
                    if n > best_n:
                        best_n, best_id, best_data = n, cid, cdata
                    break
        return best_id, best_data, best_n

    def _track_count(self, album_data: dict) -> int:
        tracks_raw = album_data.get("tracks") or []
        if isinstance(tracks_raw, list):
            return len(tracks_raw)
        return len(tracks_raw.get("items") or [])

    def _extract_cover_url(self, album_data: dict, item: dict) -> str:
        thumbs = album_data.get("thumbnails") or []
        if thumbs:
            url = thumbs[-1]["url"]
        else:
            item_thumbs = item.get("thumbnails") or []
            url = item_thumbs[-1]["url"] if item_thumbs else ""
        return _normalize_thumb_url(url)

    def _extract_track_number(self, album_data: dict, title: str) -> str:
        tracks_raw = album_data.get("tracks") or []
        track_list = tracks_raw if isinstance(tracks_raw, list) else (tracks_raw.get("items") or [])
        norm_title = TextCleaner.normalize(title)

        best_i, best_sim = None, 0.0
        for i, tr in enumerate(track_list, 1):
            tr_norm = TextCleaner.normalize(tr.get("title") or "")
            sim = similarity(norm_title, tr_norm)
            if sim > best_sim:
                best_sim = sim
                best_i = i

        return str(best_i) if best_i is not None and best_sim >= _TRACK_MATCH_MIN_SIM else ""