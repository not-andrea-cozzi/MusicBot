from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class SongMeta:
    title:          str           = ""
    artist:         str           = ""
    album:          str           = ""
    album_artist:   str           = ""
    year:           str           = ""
    genre:          str           = ""
    label:          str           = ""
    country:        str           = ""
    artist_collection:  str       = ""

    track_number:   int           = 0
    disc_number:    int           = 0
    total_tracks:   int           = 0
    total_discs:    int           = 0

    explicit:       bool          = False
    compilation:    bool          = False
    media_type:     int           = 1

    duration_ms:    Optional[int] = None

    cover_url:      str           = ""
    preview_url:    str           = ""

    isrc:           str           = ""
    upc:            str           = ""
    video_id:       str           = ""

    sort_title:          str      = ""
    sort_artist:         str      = ""
    sort_album:          str      = ""
    sort_album_artist:   str      = ""

    itunes_track_id:      str     = ""
    itunes_artist_id:     str     = ""
    itunes_collection_id: str     = ""

    mb_track_id:          str     = ""
    mb_album_id:          str     = ""
    mb_artist_id:         str     = ""
    mb_album_artist_id:   str     = ""
    mb_release_group_id:  str     = ""

    _preserved_suffix: str        = field(default="", repr=False)
    _raw_artist_full:  str        = field(default="", repr=False)
    _playlist_title:   str        = field(default="", repr=False)
    _playlist_id:      str        = field(default="", repr=False)
    _ytmusic_score:    float      = field(default=0.0, repr=False)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def set(self, key: str, value) -> None:
        if hasattr(self, key):
            setattr(self, key, value)

    def set_if_empty(self, key: str, value) -> bool:
        if not hasattr(self, key):
            return False
        current = getattr(self, key)
        is_empty = current is None or current == "" or current == 0 or current is False
        if is_empty and value not in (None, "", 0, False):
            setattr(self, key, value)
            return True
        return False

    def apply(self, data: dict, overwrite_keys: set | None = None) -> None:
        ow = overwrite_keys or set()
        for key, value in data.items():
            if not hasattr(self, key):
                continue
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)) and value == 0:
                continue
            if key in ow:
                setattr(self, key, value)
            else:
                self.set_if_empty(key, value)

    def clear_internals(self) -> None:
        self._preserved_suffix = ""
        self._raw_artist_full  = ""
        self._playlist_title   = ""
        self._playlist_id      = ""
        self._ytmusic_score    = 0.0

    _INTERNAL_FIELDS = frozenset({
        "_preserved_suffix", "_raw_artist_full",
        "_playlist_title", "_playlist_id", "_ytmusic_score",
    })

    def to_dict(self, include_internals: bool = False) -> dict:
        d = asdict(self)
        if not include_internals:
            d = {k: v for k, v in d.items() if k not in self._INTERNAL_FIELDS}
        return d

    def to_json(self, indent: int = 2, include_internals: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_internals=include_internals),
            indent=indent, ensure_ascii=False, default=str,
        )

    def dump_meta(self, logger: logging.Logger) -> None:
        lines = [f"  {k}: {v!r}" for k, v in self.to_dict(include_internals=True).items()]
        logger.error("[SongMeta] dump:\n" + "\n".join(lines))