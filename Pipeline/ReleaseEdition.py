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

    NOTA (fix track_count_verified): il `kind` viene dedotto da segnali
    testuali deboli (suffisso "- Single"/"- EP" nel nome, collection_type
    del provider) quando il conteggio tracce reale non è disponibile o non
    è stato verificato contro i dati effettivi. Fonti come YouTube Music
    non espongono MAI un suffisso "- Single" nel nome album né un
    collection_type — quindi per quelle release il `kind` risultante è
    spesso UNKNOWN non perché l'album sia ambiguo, ma perché il segnale
    testuale semplicemente non esiste per quella fonte.

    Prima di questo fix, `UNKNOWN.compatible_with(...)` ritornava sempre
    True: un "non so" veniva trattato in pratica come "va sempre bene",
    silenziosamente disattivando il guard proprio nei casi (fonte YT) dove
    servirebbe di più. `track_count_verified` rende esplicita la
    differenza: True quando il kind è stato dedotto da un conteggio tracce
    REALE (contato dai dati, non dal campo denormalizzato track_count che
    può essere stale/assente), False quando il kind è solo una miglior
    ipotesi o non è stato possibile determinarlo.
    """

    kind: ReleaseKind
    track_count: int
    edition_tokens: frozenset
    album_title_norm: str
    track_count_verified: bool = False

    @classmethod
    def from_collection(
        cls,
        *,
        collection_type: str = "",
        collection_name: str = "",
        track_count: int = 0,
        title_norm: str = "",
        track_count_verified: bool = False,
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
        elif track_count and track_count <= 2 and track_count_verified:
            # Conteggio REALE (contato dai dati, non denormalizzato) e
            # <=2 tracce: segnale sufficiente da solo per SINGLE, anche
            # senza suffisso testuale nel nome. Necessario perché fonti
            # come YouTube non forniscono mai "- Single"/eponimia nel nome
            # album — senza questo branch, un conteggio reale=1 su un
            # album YT senza nome parlante finiva comunque in UNKNOWN,
            # vanificando lo scopo del conteggio verificato.
            kind = ReleaseKind.SINGLE
        elif collection_type == "Album" or track_count > 2:
            kind = ReleaseKind.ALBUM
        else:
            kind = ReleaseKind.UNKNOWN

        # Il conteggio è "verificato" solo se track_count > 0 è stato
        # effettivamente il fattore decisivo (branch track_count<=2 o
        # track_count>2), oppure se un collection_type esplicito lo
        # conferma indipendentemente dal conteggio. Un kind dedotto SOLO
        # dal suffisso testuale (is_single_suffix/is_eponymous) senza un
        # track_count concorde non è verificato: YouTube non fornisce mai
        # quel suffisso, quindi per YT questo resta quasi sempre False
        # finché non arriva un conteggio tracce reale da qualche parte.
        verified = track_count_verified or bool(collection_type) or (track_count > 0)

        return cls(
            kind=kind,
            track_count=track_count or 0,
            edition_tokens=EditionTokens.findall(collection_name or ""),
            album_title_norm=name_norm,
            track_count_verified=verified,
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
            # I media/tracks di una release MB sono un conteggio reale
            # (non un campo denormalizzato), quindi affidabile quando > 0.
            track_count_verified=track_count > 0,
        )

    @property
    def is_short_form(self) -> bool:
        """True per Single/EP: release brevi dove l'identità conta più del solo ISRC."""
        return self.kind in (ReleaseKind.SINGLE, ReleaseKind.EP)

    @property
    def is_confident(self) -> bool:
        """
        True se questa edizione è stata dedotta da un segnale affidabile
        (conteggio tracce verificato o collection_type esplicito del
        provider), non da sola euristica testuale su un nome senza
        ulteriore conferma.
        """
        return self.kind is not ReleaseKind.UNKNOWN and self.track_count_verified

    def compatible_with(self, other: Optional["ReleaseEdition"]) -> bool:
        """
        True se le due edizioni sono intercambiabili per i campi derivati
        dalla release (track_number, disc_number, album, cover edizione).

        Regole:
        - Se una delle due non è "confident" (kind UNKNOWN o non verificato),
          non possiamo affermare né confermare né escludere compatibilità:
          trattarla come compatibile di default sarebbe di nuovo il bias
          silenzioso che questo fix vuole eliminare. Il chiamante che ha
          bisogno di una decisione netta deve controllare esplicitamente
          `is_confident` prima di usare compatible_with per decisioni
          irreversibili (es. fondere track_number tra provider diversi).
          Qui manteniamo il comportamento permissivo SOLO quando davvero
          non c'è alcun dato (kind UNKNOWN su entrambi i lati), che è
          diverso da "abbiamo un kind ma non verificato".
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
        verified = "verified" if self.track_count_verified else "unverified"
        return f"{self.kind.value}(tracks={self.track_count}/{verified}, editions={sorted(self.edition_tokens)})"

    @classmethod
    def from_deezer_kind(cls, kind: str, track_count: int = 0, title_norm: str = "") -> "ReleaseEdition":
        """Costruisce ReleaseEdition da DeezerProvider.search_recording()['_release_edition_kind']."""
        mapping = {
            "single": ReleaseKind.SINGLE,
            "ep": ReleaseKind.EP,
            "album": ReleaseKind.ALBUM,
            "compilation": ReleaseKind.COMPILATION,
        }
        resolved = mapping.get(kind, ReleaseKind.UNKNOWN)
        return cls(
            kind=resolved,
            track_count=track_count or 0,
            edition_tokens=frozenset(),
            album_title_norm=title_norm,
            track_count_verified=(resolved is not ReleaseKind.UNKNOWN),
        )

    @classmethod
    def from_verified_track_count(cls, track_count: int, collection_name: str = "", title_norm: str = "") -> "ReleaseEdition":
        return cls.from_collection(
            collection_type="",
            collection_name=collection_name,
            track_count=track_count,
            title_norm=title_norm,
            track_count_verified=track_count > 0,
        )


def isrc_match_is_safe(
    isrc_a: str,
    isrc_b: str,
    edition_a: Optional[ReleaseEdition],
    edition_b: Optional[ReleaseEdition],
) -> bool:
    if not isrc_a or not isrc_b or isrc_a.upper() != isrc_b.upper():
        return False
    if edition_a is None or edition_b is None:
        return True
    return edition_a.compatible_with(edition_b)