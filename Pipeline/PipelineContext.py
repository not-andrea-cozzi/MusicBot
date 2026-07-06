from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from Model.Song import Song
from Pipeline.PipelineResult import ITunesResult, MBResult


@dataclass
class PipelineContext:
    """
    Stato condiviso tra le fasi di MetadataPipeline.run().

    Sostituisce le ~10 variabili locali (original_title, search_isrc,
    mb_track_disc, deezer_isrc_result, sp_mapped, ...) che venivano passate
    a mano da uno step all'altro. Ogni fase legge/scrive qui invece che
    ricevere e ritornare tuple posizionali.
    """

    song: Song

    # Snapshot pre-enrichment (immutabili dopo init)
    original_title: str = ""
    original_duration_ms: Optional[int] = None

    # Esito DB — se popolato, la pipeline NON deve scrivere nulla altrove
    # per questa song (vedi `db_hit_readonly`)
    db_hit: Dict[str, Any] = field(default_factory=dict)
    db_hit_readonly: bool = False

    # Risultati provider intermedi
    spotify_mapped: Dict[str, Any] = field(default_factory=dict)
    spotify_isrc: str = ""
    mb_result: MBResult = field(default_factory=MBResult)
    mb_track_disc: Dict[str, int] = field(default_factory=dict)
    deezer_isrc_result: Dict[str, Any] = field(default_factory=dict)
    itunes_result: ITunesResult = field(default_factory=ITunesResult)
    accurate_artists: str = ""

    @classmethod
    def start(cls, song: Song) -> "PipelineContext":
        return cls(
            song=song,
            original_title=song.meta.title.strip(),
            original_duration_ms=song.meta.duration_ms,
        )

    @property
    def search_isrc(self) -> str:
        """ISRC più affidabile raccolto finora, in ordine di fiducia."""
        return self.spotify_isrc or self.mb_result.isrc or self.song.meta.isrc

    @property
    def should_skip_remote_providers(self) -> bool:
        """True se un DB hit read-only ha già risolto la song: niente iTunes/MB/Deezer."""
        return self.db_hit_readonly and bool(self.db_hit)