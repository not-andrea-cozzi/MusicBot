# Providers/DeezerProvider.py
import logging
from typing import Any, Dict, Optional

import httpx

from Algorithm.TextCleaner import TextCleaner
from Providers.Deezer.DeezerHttpClient import DeezerHttpClient
from Providers.Deezer.DeezerTextUtils import (
    is_alt_version,
    has_version_tag,
    pick_best_track,
    best_cover,
    record_type_to_edition_kind,
)


class DeezerProvider:
    """
    API pubblica invariata rispetto alla versione precedente.
    Delega HTTP/cache a DeezerHttpClient; i 4 metodi che arricchivano un
    best-match con track_data + album_data (genere/anno/UPC) ora condividono
    un unico helper privato `_enrich_from_track`.
    """

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.log = logger or logging.getLogger(__name__)
        self.http = DeezerHttpClient(client=client, timeout=timeout, logger=self.log)

    async def close(self) -> None:
        await self.http.close()


    async def _enrich_from_track(self, best: dict) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        track_id = best.get("id")
        album = best.get("album", {})
        album_id = album.get("id")

        track_data = await self.http.get_track_data(track_id) if track_id else {}
        if track_data:
            result["track_number"] = int(track_data.get("track_position", 0))
            result["disc_number"] = int(track_data.get("disk_number", 0))
            result["isrc"] = track_data.get("isrc", "")
            result["explicit"] = bool(track_data.get("explicit_lyrics", False))
            result["artist_collection"] = ", ".join(
                c.get("name", "") for c in track_data.get("contributors", []) if c.get("name")
            )

        if album_id:
            album_data = await self.http.get_album_data(album_id)
            if album_data:
                genres = album_data.get("genres", {}).get("data", [])
                if genres:
                    result["genre"] = genres[0].get("name", "").title()
                rd = album_data.get("release_date", "")
                if rd and len(rd) >= 4:
                    result["year"] = rd[:4]
                if album_data.get("upc"):
                    result["upc"] = album_data["upc"]
                result["_release_edition_kind"] = record_type_to_edition_kind(
                    album_data.get("record_type", ""), album_data.get("nb_tracks", 0)
                )

        return result

    # ------------------------------------------------------------------
    # ISRC diretto
    # ------------------------------------------------------------------
    async def get_by_isrc(self, isrc: str) -> dict:
        if not isrc:
            return {}

        data = await self.http.get_isrc(isrc)
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
            "cover_url":         best_cover(album),
            "duration_ms":       int(data.get("duration", 0)) * 1000,
            "track_number":      int(data.get("track_position", 0)),
            "disc_number":       int(data.get("disk_number", 0)),
            "isrc":              data.get("isrc", isrc),
            "explicit":          bool(data.get("explicit_lyrics", False)),
            "year":              (data.get("release_date") or "")[:4],
        }

        if album_id:
            album_data = await self.http.get_album_data(album_id)
            if album_data:
                genres = album_data.get("genres", {}).get("data", [])
                if genres:
                    result["genre"] = genres[0].get("name", "").title()
                if not result.get("year"):
                    rd = album_data.get("release_date", "")
                    if rd and len(rd) >= 4:
                        result["year"] = rd[:4]
                if album_data.get("upc"):
                    result["upc"] = album_data["upc"]

        self.log.debug(f"[Deezer] ISRC {isrc} -> '{result['title']}' genre={result.get('genre')!r}")
        return result

    # ------------------------------------------------------------------
    # Cover / genre / track+disc (query mirate, invariate)
    # ------------------------------------------------------------------
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
            tracks = await self.http.search_with_fallback(10, **fields)
            best = pick_best_track(tracks, album)
            if best:
                cover = best_cover(best.get("album", {}))
                if cover:
                    return cover
        return None

    async def get_genre(self, title: str, artist: str) -> str:
        if not title or not artist:
            return ""
        try:
            data = await self.http.search_with_fallback(5, track=title, artist=artist)
            if not data:
                return ""
            best = pick_best_track(data, "")
            if not best:
                return ""
            album_id = best.get("album", {}).get("id")
            if not album_id:
                return ""
            album_data = await self.http.get_album_data(album_id)
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
            data = await self.http.search_with_fallback(5, track=title, artist=artist, album=album)
            if not data:
                return 0, 0
            best = pick_best_track(data, album)
            if not best:
                return 0, 0
            track_id = best.get("id")
            if not track_id:
                return 0, 0
            track_data = await self.http.get_track_data(track_id)
            return int(track_data.get("track_position", 0)), int(track_data.get("disk_number", 0))
        except Exception as exc:
            self.log.warning(f"[Deezer] get_track_and_disc fallito: {exc}")
        return 0, 0

    # ------------------------------------------------------------------
    # Full metadata / search-by-title-artist — ora via _enrich_from_track
    # ------------------------------------------------------------------
    async def get_full_metadata(self, title: str, artist: str, album: str = "") -> dict:
        if not title and not artist:
            return {}
        try:
            data = await self.http.search_with_fallback(5, track=title, artist=artist, album=album)
            if not data:
                return {}
            best = pick_best_track(data, album)
            if not best:
                return {}

            result = {
                "title":       best.get("title", ""),
                "artist":      best.get("artist", {}).get("name", ""),
                "album":       best.get("album", {}).get("title", ""),
                "cover_url":   best_cover(best.get("album", {})),
                "duration_ms": int(best.get("duration", 0)) * 1000,
            }
            result.update(await self._enrich_from_track(best))
            result.pop("artist_collection", None)  # non richiesto da questo metodo (comportamento originale)
            result.pop("_release_edition_kind", None)
            return result
        except Exception as exc:
            self.log.warning(f"[Deezer] get_full_metadata fallito: {exc}")
        return {}

    async def search_by_title_artist(self, title: str, artist: str) -> dict:
        if not title or not artist:
            return {}
        data = await self.http.search_with_fallback(10, track=title, artist=artist)
        best = pick_best_track(data, "")
        if not best:
            return {}

        enriched = await self._enrich_from_track(best)
        if not enriched.get("isrc"):
            return {}

        album = best.get("album", {})
        result = {
            "isrc":              enriched.get("isrc", ""),
            "title":             best.get("title", ""),
            "artist":            best.get("artist", {}).get("name", ""),
            "artist_collection": enriched.get("artist_collection", ""),
            "album":             album.get("title", ""),
            "cover_url":         best_cover(album),
            "duration_ms":       int(best.get("duration", 0)) * 1000,
            "track_number":      enriched.get("track_number", 0),
            "disc_number":       enriched.get("disc_number", 0),
            "explicit":          enriched.get("explicit", False),
            "year":              enriched.get("year", ""),
            "genre":             enriched.get("genre", ""),
        }
        if enriched.get("upc"):
            result["upc"] = enriched["upc"]
        return result

    # ------------------------------------------------------------------
    # search_recording — usato da MetadataPipeline come recording match
    # ------------------------------------------------------------------
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
        Ricerca strutturata: ISRC diretto -> search testuale con filtro alt-version.
        Ritorna dict "raw" compatibile con MetaMapper.from_deezer_isrc, con chiavi extra:
          - "_release_edition_kind", "_alt_version_rejected", "upc" (se disponibile)
        """
        if not title:
            return {}

        title_has_remix = has_version_tag(title)
        alt_rejected = False

        # 1. ISRC diretto
        if isrc:
            raw = await self.get_by_isrc(isrc)
            if raw:
                cand_title = raw.get("title", "")
                if is_alt_version(cand_title) and not is_alt_version(title):
                    self.log.debug(f"[Deezer] ISRC {isrc} -> alt-version scartata: {cand_title!r}")
                    alt_rejected = True
                elif title_has_remix and not has_version_tag(cand_title):
                    self.log.debug(f"[Deezer] ISRC {isrc} -> tag versione incoerente: {cand_title!r}")
                else:
                    raw["_release_edition_kind"] = "unknown"  # get_by_isrc non espone record_type direttamente
                    raw["_alt_version_rejected"] = False
                    return raw

        # 2. Search testuale
        data = await self.http.search_with_fallback(15, track=title, artist=artist, album=album_hint)
        if not data:
            return {}

        best, best_score = None, -1.0
        for t in data:
            t_title = t.get("title", "")

            if is_alt_version(t_title) and not is_alt_version(title):
                alt_rejected = True
                continue

            if title_has_remix and not has_version_tag(t_title):
                continue

            title_sim = TextCleaner.title_similarity(title, t_title)
            if title_sim < 0.55:
                continue

            t_artist = (t.get("artist") or {}).get("name", "")
            artist_sim = TextCleaner.title_similarity(artist, t_artist) if artist else 1.0
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

        # Fallback: se il tag era obbligatorio e nessun candidato lo aveva, riprova senza vincolo tag.
        if not best and title_has_remix:
            for t in data:
                t_title = t.get("title", "")
                if is_alt_version(t_title) and not is_alt_version(title):
                    continue
                title_sim = TextCleaner.title_similarity(title, t_title)
                if title_sim < 0.55:
                    continue
                if title_sim > best_score:
                    best_score, best = title_sim, t

        if not best or best_score < min_score:
            return {}

        album = best.get("album", {})
        result = {
            "title":             best.get("title", ""),
            "artist":            best.get("artist", {}).get("name", ""),
            "album":             album.get("title", ""),
            "cover_url":         best_cover(album),
            "duration_ms":       int(best.get("duration", 0)) * 1000,
            "_alt_version_rejected": alt_rejected,
        }

        enriched = await self._enrich_from_track(best)
        result["artist_collection"] = enriched.get("artist_collection", "")
        result["isrc"] = enriched.get("isrc", "")
        result["explicit"] = enriched.get("explicit", False)
        for k in ("track_number", "disc_number", "genre", "year", "upc", "_release_edition_kind"):
            if k in enriched:
                result[k] = enriched[k]

        return result