from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from Pipeline.ReleaseEdition import ReleaseEdition


class MatchConfidence(float, Enum):
    """
    Livelli di confidenza semantici.
    Sono float sottoclassati così possono essere confrontati direttamente
    con soglie numeriche (es. confidence >= MatchConfidence.GOOD).
    """
    ISRC_EXACT   = 1.00   # Match ISRC: identità certa del *recording* (non della release)
    HIGH         = 0.90   # Titolo + artista + durata coerenti
    GOOD         = 0.75   # Titolo + artista ok, durata o album leggermente diversi
    LOW          = 0.55   # Solo titolo ok, artista o durata discordanti
    NONE         = 0.00   # Nessun risultato


@dataclass
class MBResult:
    """Risultato dello step MusicBrainz con metadati di confidenza."""

    recording: Optional[dict] = None        # recording detail completo
    album: Optional[dict] = None            # album detail completo
    track_score: float = 0.0                # score TrackMatcher sulla traccia
    album_score: float = 0.0                # score album_edition_similarity
    confidence: MatchConfidence = MatchConfidence.NONE
    isrc: str = ""                          # ISRC estratto da MB (se presente)

    # flag diagnostici
    album_is_deluxe: bool = False           # album trovato è edizione deluxe/expanded
    title_has_remix: bool = False           # titolo originale contiene "(Remix)" o simili

    # NUOVO: identità di edizione (single/EP/album) della release scelta.
    # Usato per decidere se un match ISRC con un altro provider è "sicuro"
    # per fondere track_number/disc_number/album, o se le due release sono
    # semplicemente diverse pur condividendo il recording.
    release_edition: Optional[ReleaseEdition] = None

    @property
    def found(self) -> bool:
        return self.recording is not None

    @property
    def mb_title(self) -> str:
        """Titolo MB grezzo (non pulito) della recording."""
        return (self.recording or {}).get("title", "")

    @property
    def mb_album_title(self) -> str:
        return (self.album or {}).get("title", "")

    def __repr__(self) -> str:
        edition = self.release_edition.describe() if self.release_edition else "n/a"
        return (
            f"MBResult(confidence={self.confidence.name}, "
            f"track_score={self.track_score:.2f}, "
            f"album_score={self.album_score:.2f}, "
            f"isrc={self.isrc!r}, "
            f"edition={edition}, "
            f"title={self.mb_title!r})"
        )


@dataclass
class ITunesResult:
    """Risultato dello step iTunes con metadati di confidenza."""

    data: dict = field(default_factory=dict)
    confidence: MatchConfidence = MatchConfidence.NONE
    matched_by_isrc: bool = False

    # Usati per la validazione incrociata con MB
    itunes_track_number: Optional[int] = None
    itunes_disc_number: Optional[int] = None
    itunes_duration_ms: Optional[int] = None
    itunes_album: str = ""

    # NUOVO: stessa semantica di MBResult.release_edition
    release_edition: Optional[ReleaseEdition] = None

    # NUOVO: True se questo risultato proviene dalla cache locale (DB) e
    # quindi non deve generare scritture di persistenza a valle.
    from_db: bool = False

    @property
    def found(self) -> bool:
        return bool(self.data)

    def __repr__(self) -> str:
        edition = self.release_edition.describe() if self.release_edition else "n/a"
        return (
            f"ITunesResult(confidence={self.confidence.name}, "
            f"by_isrc={self.matched_by_isrc}, "
            f"track={self.itunes_track_number}, "
            f"edition={edition}, "
            f"from_db={self.from_db}, "
            f"album={self.itunes_album!r})"
        )