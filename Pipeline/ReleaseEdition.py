from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from Algorithm.RegexToken import EditionTokens, FakeAlbumSuffix


class ReleaseKind(Enum):
    """Tipo di release a cui appartiene una traccia."""
    SINGLE = "single"
    EP = "ep"
    ALBUM = "album"
    COMPILATION = "compilation"
    UNKNOWN = "unknown"


_SINGLE_EP_SUFFIX_RE = re.compile(r"\s*-\s*(single|ep)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseEdition:
    """
    Identità di edizione di una release, indipendente dal provider (MB/iTunes/Deezer/DB).

    Stesso ISRC NON implica stessa ReleaseEdition: un brano può uscire come
    singolo e poi essere incluso in un album (o in una deluxe). Due risultati
    con lo stesso ISRC ma kind/edition_tokens differenti sono release diverse
    e NON devono essere fuse automaticamente solo perché l'ISRC matcha.
    """

    kind: ReleaseKind
    track_count: int
    edition_tokens: frozenset
    album_title_norm: str

    @classmethod
    def from_collection(
        cls,
        *,
        collection_type: str = "",
        collection_name: str = "",
        track_count: int = 0,
        title_norm: str = "",
    ) -> "ReleaseEdition":
        name_norm = (collection_name or "").strip().lower()
        is_single_suffix = bool(_SINGLE_EP_SUFFIX_RE.search(name_norm))
        is_eponymous = bool(title_norm) and FakeAlbumSuffix.strip(name_norm) == title_norm

        if collection_type == "Compilation":
            kind = ReleaseKind.COMPILATION
        elif collection_type in ("Single",) or (is_single_suffix and not collection_type):
            kind = ReleaseKind.SINGLE
        elif collection_type == "EP" or "ep" in name_norm.split("-")[-1:]:
            kind = ReleaseKind.EP
        elif track_count and track_count <= 2 and (is_single_suffix or is_eponymous):
            kind = ReleaseKind.SINGLE
        elif collection_type == "Album" or track_count > 2:
            kind = ReleaseKind.ALBUM
        else:
            kind = ReleaseKind.UNKNOWN

        return cls(
            kind=kind,
            track_count=track_count or 0,
            edition_tokens=EditionTokens.findall(collection_name or ""),
            album_title_norm=name_norm,
        )

    @classmethod
    def from_mb_release(cls, release: dict, title_norm: str = "") -> "ReleaseEdition":
        media = release.get("media", [])
        track_count = sum(len(m.get("tracks", []) or []) for m in media) if media else 0
        rg = release.get("release-group", {}) or {}
        primary_type = (rg.get("primary-type") or "").lower()

        collection_type = {
            "single": "Single",
            "ep": "EP",
            "album": "Album",
        }.get(primary_type, "")

        return cls.from_collection(
            collection_type=collection_type,
            collection_name=release.get("title", ""),
            track_count=track_count,
            title_norm=title_norm,
        )

    @property
    def is_short_form(self) -> bool:
        """True per Single/EP: release brevi dove l'identità conta più del solo ISRC."""
        return self.kind in (ReleaseKind.SINGLE, ReleaseKind.EP)

    def compatible_with(self, other: Optional["ReleaseEdition"]) -> bool:
        """
        True se le due edizioni sono intercambiabili per i campi derivati
        dalla release (track_number, disc_number, album, cover edizione).

        Regole:
        - UNKNOWN è sempre compatibile (nessun dato per contraddire).
        - Single/EP vs Album/Compilation → NON compatibili (stesso ISRC,
          release diversa: niente da fondere automaticamente).
        - Edition token divergenti (deluxe vs standard, ecc.) → NON compatibili.
        - Stesso kind "ampio" (Album/Compilation tra loro) → compatibili se
          edition token coincidono o sono entrambi vuoti.
        """
        if other is None or self.kind is ReleaseKind.UNKNOWN or other.kind is ReleaseKind.UNKNOWN:
            return True

        if self.is_short_form != other.is_short_form:
            return False

        if self.edition_tokens != other.edition_tokens:
            return False

        return True

    def describe(self) -> str:
        return f"{self.kind.value}(tracks={self.track_count}, editions={sorted(self.edition_tokens)})"
    
    @classmethod
    def from_deezer_kind(cls, kind: str, track_count: int = 0, title_norm: str = "") -> "ReleaseEdition":
        """Costruisce ReleaseEdition da DeezerProvider.search_recording()['_release_edition_kind']."""
        mapping = {
            "single": ReleaseKind.SINGLE,
            "ep": ReleaseKind.EP,
            "album": ReleaseKind.ALBUM,
            "compilation": ReleaseKind.COMPILATION,
        }
        return cls(
            kind=mapping.get(kind, ReleaseKind.UNKNOWN),
            track_count=track_count or 0,
            edition_tokens=frozenset(),
            album_title_norm=title_norm,
        )


def isrc_match_is_safe(
    isrc_a: str,
    isrc_b: str,
    edition_a: Optional[ReleaseEdition],
    edition_b: Optional[ReleaseEdition],
) -> bool:
    """
    True se un match per ISRC identico può essere usato per fondere/sovrascrivere
    campi specifici-di-release (track_number, disc_number, album, cover) tra due
    risultati. False se l'ISRC è identico ma le release sono di tipo diverso
    (es. single trovato su un provider, album trovato su un altro): in quel caso
    l'ISRC resta valido come identificatore del *recording*, ma i metadati di
    release vanno trattati come provenienti da edizioni diverse.
    """
    if not isrc_a or not isrc_b or isrc_a.upper() != isrc_b.upper():
        return False
    if edition_a is None or edition_b is None:
        return True
    return edition_a.compatible_with(edition_b)

    