# Model/SongMetaWriter.py
from __future__ import annotations

import logging
from typing import Optional

import httpx
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from Model.SongMeta import SongMeta


class SongMetaWriter:

    log = logging.getLogger(__name__)

    @classmethod
    def write(
        cls,
        path: str,
        meta: SongMeta,
        cover_bytes: Optional[bytes] = None,
        cover_url: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        tags = cls.build_tags(meta)
        cover = cover_bytes or cls._fetch_cover(cover_url or meta.cover_url, timeout=timeout)
        cls._write_to_file(path, tags, cover)
        cls.log.debug(f"[SongMetaWriter] Tag scritti su '{path}' ({len(tags)} atomi).")

    @staticmethod
    def _freeform(val: str) -> list[MP4FreeForm]:
        return [MP4FreeForm(val.encode("utf-8"))]

    @classmethod
    def build_tags(cls, m: SongMeta) -> dict:
        tags: dict = {}

        text_atoms = (
            ("\xa9nam", "title"),
            ("\xa9ART", "artist_collection"),
            ("aART",    "album_artist"),
            ("\xa9alb", "album"),
            ("\xa9gen", "genre"),
            ("\xa9wrt", "composer"),
        )
        for atom, attr in text_atoms:
            v = getattr(m, attr, "") or ""
            if not v and attr == "artist_collection":
                v = m.artist
            if v:
                tags[atom] = [v]

        if m.year and m.year.isdigit() and 1900 <= int(m.year) <= 2100:
            tags["\xa9day"] = [m.year]

        sort_atoms = (
            ("sonm", "sort_title"),
            ("soar", "sort_artist"),
            ("soal", "sort_album"),
            ("soaa", "sort_album_artist"),
        )
        for atom, attr in sort_atoms:
            v = getattr(m, attr, "")
            if v:
                tags[atom] = [v]

        # ── Track / disc (fix: niente "(n, 0)" quando total ignoto) ─────
        if m.track_number > 0:
            tags["trkn"] = [(m.track_number, m.total_tracks)] if m.total_tracks > 0 else [(m.track_number, 0)]
        if m.disc_number > 0:
            tags["disk"] = [(m.disc_number, m.total_discs)] if m.total_discs > 0 else [(m.disc_number, 0)]

        if m.compilation:
            tags["cpil"] = [1]
        tags["rtng"] = [4 if m.explicit else 0]
        tags["stik"] = [m.media_type]
        tags["pgap"] = [0]

        # ── Label / copyright (fix: cprt separato da label) ─────────────
        if m.label:
            tags["\xa9lab"] = [m.label]
            tags["----:com.apple.iTunes:LABEL"] = cls._freeform(m.label)

        if m.copyright:
            tags["cprt"] = [m.copyright]
        elif m.label and m.year:
            tags["cprt"] = [f"℗ {m.year} {m.label}"]

        if m.upc:
            tags["----:com.apple.iTunes:BARCODE"] = cls._freeform(m.upc)
        if m.country:
            tags["----:com.apple.iTunes:MusicBrainz Album Release Country"] = cls._freeform(m.country)

        if m.isrc:
            tags["\xa9isr"] = [m.isrc]
            tags["----:com.apple.iTunes:ISRC"] = cls._freeform(m.isrc)

        itunes_ids = (
            ("itunes_track_id",      "iTunes_CDDB_TrackNumber"),
            ("itunes_artist_id",     "iTunes_CDDB_ArtistID"),
            ("itunes_collection_id", "iTunes_CDDB_CollectionID"),
        )
        for key, atom in itunes_ids:
            v = getattr(m, key, "")
            if v:
                tags[f"----:com.apple.iTunes:{atom}"] = cls._freeform(str(v))

        mb_ids = (
            ("mb_track_id",         "MusicBrainz Track Id"),
            ("mb_album_id",         "MusicBrainz Album Id"),
            ("mb_artist_id",        "MusicBrainz Artist Id"),
            ("mb_album_artist_id",  "MusicBrainz Album Artist Id"),
            ("mb_release_group_id", "MusicBrainz Release Group Id"),
        )
        for key, atom in mb_ids:
            v = getattr(m, key, "")
            if v:
                tags[f"----:com.apple.iTunes:{atom}"] = cls._freeform(v)

        return tags

    @classmethod
    def _fetch_cover(cls, url: Optional[str], timeout: float = 10.0) -> Optional[bytes]:
        if not url:
            return None
        url = cls._upscale_cover_url(url)
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(url)
                r.raise_for_status()
                return r.content
        except Exception as exc:
            cls.log.debug(f"[SongMetaWriter] Cover fetch fallita: {exc}")
            return None

    @staticmethod
    def _upscale_cover_url(url: str) -> str:
        if "mzstatic.com" in url:
            import re
            url = re.sub(r"/\d+x\d+(bb)?\.(jpg|png)", r"/3000x3000\1.\2", url)
        return url

    @staticmethod
    def _write_to_file(path: str, tags: dict, cover: Optional[bytes]) -> None:
        audio = MP4(path)
        audio.delete()
        audio.update(tags)
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
        audio.save()