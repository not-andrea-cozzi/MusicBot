from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from Model.SongMeta import SongMeta
from Model.SongMetaWriter import SongMetaWriter


class SongStatus(Enum):
    DOWNLOADING = "downloading"
    TAGGING     = "tagging"
    DONE        = "done"
    ERROR       = "error"


@dataclass
class Song:
    song_id:  str = field(default_factory=lambda: str(uuid.uuid4()))
    video_id: str = ""

    path_original: str       = ""
    path_current:  str       = ""
    path_final:    str       = ""
    path_history:  list[str] = field(default_factory=list)

    status: SongStatus    = SongStatus.DOWNLOADING
    error:  Optional[str] = None

    raw:  dict     = field(default_factory=dict)
    meta: SongMeta = field(default_factory=SongMeta)

    def set_path(self, new_path: str) -> None:
        if new_path and new_path != self.path_current:
            if self.path_current:
                self.path_history.append(self.path_current)
            self.path_current = new_path

    def finalize_path(self, final_path: str) -> None:
        self.set_path(final_path)
        self.path_final = final_path
        self.status     = SongStatus.DONE

    def mark_tagging(self) -> None:
        self.status = SongStatus.TAGGING

    def mark_error(self, reason: str) -> None:
        self.status = SongStatus.ERROR
        self.error  = reason

    def write_tags(
        self, path: Optional[str] = None,
        cover_override: Optional[bytes] = None, cover_url: Optional[str] = None,
    ) -> None:
        target = path or self.path_current
        if not target:
            raise ValueError("Nessun path disponibile per write_tags()")
        SongMetaWriter.write(
            path=target, meta=self.meta, cover_bytes=cover_override, cover_url=cover_url,
        )

    @staticmethod
    def build_sort_name(name: str, lang: str = "en") -> str:
        articles = {
            "en": ["the", "a", "an"],
            "it": ["il", "lo", "la", "i", "gli", "le", "l'", "un", "una"],
            "es": ["el", "la", "los", "las", "un", "una"],
            "de": ["der", "die", "das", "ein", "eine"],
            "fr": ["le", "la", "les", "l'", "un", "une", "des"],
        }
        lower = name.lower()
        for art in articles.get(lang, articles["en"]):
            if lower.startswith(art + " "):
                return f"{name[len(art) + 1:]}, {name[:len(art)]}"
        return name

    def dump_meta(self, logger: logging.Logger) -> None:
        self.meta.dump_meta(logger)

    def to_json(self, indent: int = 2) -> str:
        return self.meta.to_json(indent=indent)