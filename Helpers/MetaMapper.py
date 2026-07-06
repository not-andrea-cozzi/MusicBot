from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from Algorithm.TextCleaner import TextCleaner


class MetaMapper:

    @staticmethod
    def from_itunes(
        item: Dict[str, Any], default_title: str = "", default_artist: str = "",
        logger: Optional[logging.Logger] = None,
    ) -> Dict[str, Any]:
        import re

        log = logger or logging.getLogger(__name__)

        raw_url = item.get("artworkUrl100", "")
        cover_url = re.sub(r"\d+x\d+bb", "3000x3000bb", raw_url) if raw_url else ""

        year = (item.get("releaseDate") or "")[:4]
        if not year or year == "0":
            year = ""

        track_number = int(item["trackNumber"]) if item.get("trackNumber") else 0
        disc_number  = int(item["discNumber"])  if item.get("discNumber")  else 0

        explicit_raw = item.get("trackExplicitness", "")
        is_explicit  = explicit_raw  in ("explicit", "cleaned")

        raw_album_artist = (
            item.get("collectionArtistName", "")
            or item.get("artistName", "")
            or default_artist
        )

        norm_aa = TextCleaner.normalize(raw_album_artist)
        norm_pa = TextCleaner.normalize(
            TextCleaner.primary_artist(item.get("artistName", "") or default_artist)
        )
        is_various   = norm_aa in ("various artists", "aa.vv.", "artisti vari")
        is_group     = bool(raw_album_artist) and bool(norm_pa) and norm_aa != norm_pa
        is_itunes_compilation = (
            item.get("collectionType") == "Compilation"
            and TextCleaner.normalize(item.get("collectionArtistName", "")) == "various artists"
        )
        is_compilation = is_various or is_group or is_itunes_compilation

        log.debug(
            f"[MetaMapper][iTunes] album_artist='{raw_album_artist}' "
            f"compilation={is_compilation} explicit={is_explicit}"
        )

        return {
            "title":                item.get("trackName",           default_title),
            "artist":               item.get("artistName",          default_artist),
            "album_artist":         raw_album_artist,
            "album":                item.get("collectionName",      ""),
            "year":                 year,
            "track_number":         track_number,
            "disc_number":          disc_number,
            "cover_url":            cover_url,
            "genre":                item.get("primaryGenreName",    ""),
            "itunes_track_id":      str(item.get("trackId")        or ""),
            "itunes_artist_id":     str(item.get("artistId")       or ""),
            "itunes_collection_id": str(item.get("collectionId")   or ""),
            "preview_url":          item.get("previewUrl",          ""),
            "explicit":             is_explicit,
            "compilation":          is_compilation,
            "duration_ms":          item.get("trackTimeMillis"),
            "isrc":                 item.get("isrc", ""),
        }

    @staticmethod
    def from_mb_recording(
        recording: Dict[str, Any], album: Optional[Dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> Dict[str, Any]:
        log = logger or logging.getLogger(__name__)

        releases    = recording.get("releases", [])
        first_rel   = releases[0] if releases else {}
        rel_group   = first_rel.get("release-group", {})
        art_credits = recording.get("artist-credit", [])

        artist_name = "".join(
            ac.get("name", "") + ac.get("joinphrase", "")
            for ac in art_credits
            if isinstance(ac, dict)
        ).strip()

        isrcs = recording.get("isrcs", [])

        date_str = (
            recording.get("first-release-date", "")
            or first_rel.get("date", "")
            or (album or {}).get("date", "")
        )
        year = date_str[:4] if date_str and len(date_str) >= 4 else ""

        album_title = (
            first_rel.get("title", "")
            or (album or {}).get("title", "")
        )

        label = ""
        label_info = first_rel.get("label-info", [])
        if label_info:
            label = label_info[0].get("label", {}).get("name", "")

        _FAKE = {"XW", "XE", "XU", "XX"}
        country_raw = (first_rel.get("country") or "").strip().upper()
        country = country_raw if (country_raw and len(country_raw) == 2 and country_raw not in _FAKE) else ""

        def _best_tag(entity: Dict) -> str:
            genres = entity.get("genres", [])
            if genres:
                return max(genres, key=lambda x: x.get("count", 0)).get("name", "")
            tags = entity.get("tags", [])
            if tags:
                return max(tags, key=lambda x: x.get("count", 0)).get("name", "")
            return ""

        genre = _best_tag(recording) or _best_tag(rel_group) or _best_tag(first_rel)
        if genre:
            genre = genre.title()

        result = {
            "title":               TextCleaner.clean_text(recording.get("title", ""), field_type="title"),
            "artist":              TextCleaner.clean_text(artist_name, field_type="artist"),
            "album":               album_title,
            "year":                year,
            "genre":               genre,
            "isrc":                isrcs[0] if isrcs else "",
            "label":               label,
            "country":             country,
            "mb_track_id":         recording.get("id", ""),
            "mb_album_id":         first_rel.get("id", ""),
            "mb_release_group_id": rel_group.get("id", ""),
        }

        for ac in art_credits:
            if isinstance(ac, dict):
                aid = ac.get("artist", {}).get("id", "")
                if aid:
                    result["mb_artist_id"] = aid
                    break

        for ac in first_rel.get("artist-credit", []):
            if isinstance(ac, dict):
                aid = ac.get("artist", {}).get("id", "")
                if aid:
                    result["mb_album_artist_id"] = aid
                    break

        log.debug(f"[MetaMapper][MB] {result}")
        return result

    @staticmethod
    def from_mb_track(
        media: Dict[str, Any], track: Dict[str, Any], all_tracks: list,
        logger: Optional[logging.Logger] = None,
    ) -> Dict[str, Any]:
        from Helpers.MusicBrainzHelper import MusicBrainzHelper

        result: Dict[str, Any] = {}

        raw_number = track.get("number", "")
        if raw_number:
            tn = MusicBrainzHelper.parse_track_number(raw_number, all_tracks)
            if tn:
                result["track_number"] = tn

        dn = media.get("position")
        if dn:
            try:
                result["disc_number"] = int(dn)
            except (ValueError, TypeError):
                pass

        return result

    @staticmethod
    def from_deezer(data: Dict[str, Any], logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
        log = logger or logging.getLogger(__name__)

        result: Dict[str, Any] = {}

        _FIELD_MAP = {
            "title": "title", "album": "album", "cover_url": "cover_url",
            "duration_ms": "duration_ms", "track_number": "track_number",
            "disc_number": "disc_number", "genre": "genre", "year": "year",
        }

        for src_key, meta_key in _FIELD_MAP.items():
            val = data.get(src_key)
            if val is None or val == "":
                continue
            if isinstance(val, int) and val == 0:
                continue
            result[meta_key] = val
            log.debug(f"[MetaMapper][Deezer] {meta_key} = {val!r}")

        return result

    @staticmethod
    def from_ytmusic(raw: dict, score: float = 1.0, logger: Optional[logging.Logger] = None) -> dict:
        log = logger or logging.getLogger(__name__)

        result: dict = {}
        for key in ("title", "artist", "album", "year", "track_number", "cover_url", "isrc"):
            val = raw.get(key)
            if val is None or val == "":
                continue
            if isinstance(val, int) and val == 0:
                continue
            result[key] = val

        if score != 1.0:
            result["_ytmusic_score"] = score

        log.debug(f"[MetaMapper][YTMusic] {result}")
        return result

    @staticmethod
    def from_deezer_isrc(data: dict, logger=None) -> dict:
        log = logger or logging.getLogger(__name__)
        out = MetaMapper.from_deezer(data, logger=log)
        if data.get("isrc"):
            out["isrc"] = data["isrc"]
        return out