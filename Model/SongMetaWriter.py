# Model/SongMetaWriter.py
from __future__ import annotations

import logging
import re
import subprocess
from typing import Optional

import httpx
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from Model.SongMeta import SongMeta


STOREFRONT_IDS = {
    "US": 143441, "GB": 143444, "CA": 143455, "AU": 143460,
    "IT": 143450, "FR": 143442, "DE": 143443, "ES": 143454,
    "PT": 143453, "NL": 143452, "BE": 143446, "CH": 143459,
    "AT": 143445, "SE": 143456, "NO": 143457, "DK": 143458,
    "FI": 143447, "PL": 143478, "RU": 143469, "GR": 143448,
    "TR": 143480, "IE": 143449, "JP": 143462, "KR": 143466,
    "CN": 143465, "TW": 143470, "HK": 143463, "IN": 143467,
    "ID": 143476, "PH": 143474, "MY": 143473, "SG": 143464,
    "TH": 143475, "VN": 143471, "ZA": 143472, "MX": 143468,
    "BR": 143503, "AR": 143505, "CL": 143483, "CO": 143501,
    "AE": 143481, "SA": 143479, "IL": 143491, "NZ": 143461,
    "RO": 143487, "HU": 143482, "CZ": 143489, "SK": 143496,
    "BG": 143526, "HR": 143494, "SI": 143499,
}


def get_store_code(country_code: str, default: int = 143441) -> int:
    return STOREFRONT_IDS.get((country_code or "").upper(), default)


class SongMetaWriter:

    log = logging.getLogger("ScariMusicBot")
    ATOMICPARSLEY_BIN = "AtomicParsley"

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
        cls._apply_apple_atoms(path, meta)
        cls.log.debug(f"[SongMetaWriter] Tag scritti su '{path}' ({len(tags)} atomi).")

    @staticmethod
    def _freeform(val: str) -> list[MP4FreeForm]:
        return [MP4FreeForm(val.encode("utf-8"))]

    @classmethod
    def build_tags(cls, m: SongMeta) -> dict:
        tags: dict = {}

        text_atoms = (
            ("\xa9nam", "title"),
            ("\xa9ART", "artist"),
            ("aART",    "album_artist"),
            ("\xa9alb", "album"),
            ("\xa9gen", "genre"),
            ("\xa9wrt", "composer"),
            ("\xa9grp", "grouping"),
            ("desc",    "description"),
            ("purd",    "purchase_date"),
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
            ("sonm", "sort_title"), ("soar", "sort_artist"),
            ("soal", "sort_album"), ("soaa", "sort_album_artist"),
        )
        for atom, attr in sort_atoms:
            v = getattr(m, attr, "")
            if v:
                tags[atom] = [v]

        if m.track_number > 0:
            tags["trkn"] = [(m.track_number, m.total_tracks)] if m.total_tracks > 0 else [(m.track_number, 0)]
        if m.disc_number > 0:
            tags["disk"] = [(m.disc_number, m.total_discs)] if m.total_discs > 0 else [(m.disc_number, 0)]

        if m.compilation:
            tags["cpil"] = [1]
        tags["rtng"] = [4 if m.explicit else 0]
        tags["stik"] = [m.media_type]
        tags["pgap"] = [0]
        tags["akID"] = [1]  # 1 = iTunes Store account

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

        apple_internal = (
            ("itunes_collection_id", "plID"),
            ("itunes_collection_id", "cnID"),
            ("itunes_artist_id",     "atID"),
        )
        for key, atom in apple_internal:
            v = getattr(m, key, "")
            if v and str(v).isdigit():
                tags[atom] = [int(v)]

        if m.itunes_track_id and str(m.itunes_track_id).isdigit():
            tags["cmID"] = [int(m.itunes_track_id)]
        tags["sfID"] = [get_store_code(m.country)]
        tags["\xa9too"] = ["iTunes 12.12.0.1"]

        return tags

    @classmethod
    def _apply_apple_atoms(cls, path: str, m: SongMeta) -> None:
        """purl/ldes non gestiti da mutagen: richiedono AtomicParsley."""
        purl = getattr(m, "purchase_url", "")
        ldes = getattr(m, "long_description", "")
        if not purl and not ldes:
            return

        args = [cls.ATOMICPARSLEY_BIN, path, "--overWrite"]
        if purl:
            args += ["--purl", purl]
        if ldes:
            args += ["--ldes", ldes]

        try:
            subprocess.run(args, check=True, capture_output=True, timeout=15)
            cls.log.error("[SongMetaWriter] AtomicParsley installato.")
        except FileNotFoundError:
            cls.log.error("[SongMetaWriter] AtomicParsley non installato.")
        except subprocess.CalledProcessError as exc:
            cls.log.error(f"[SongMetaWriter] AtomicParsley fallito: {exc.stderr}")
        except subprocess.TimeoutExpired:
            cls.log.error("[SongMetaWriter] AtomicParsley timeout.")

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