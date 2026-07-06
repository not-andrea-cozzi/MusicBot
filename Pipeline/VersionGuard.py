from __future__ import annotations

import re
import logging
from typing import Optional

from Algorithm.TextCleaner import TextCleaner
from Algorithm.BestMatch import strip_parenthetical
from Pipeline.PipelineResult import MBResult, ITunesResult, MatchConfidence
from Pipeline.ReleaseEdition import isrc_match_is_safe


# Regex per identificare tag di versione nel titolo
_REMIX_TAG_RE   = re.compile(r'\b(?:remix|re-?mix)\b', re.IGNORECASE)
_VERSION_TAG_RE = re.compile(
    r'[\(\[]\s*(?:remix|re-?mix|radio\s+edit|extended|vip|club\s+mix|'
    r'dub\s+mix|original\s+mix|acoustic|live|demo|instrumental)\b',
    re.IGNORECASE,
)
_DELUXE_TAG_RE  = re.compile(
    r'\b(?:deluxe|expanded|super\s+deluxe|anniversary|remastered|'
    r'special\s+edition|bonus\s+track)\b',
    re.IGNORECASE,
)

# Tolleranza durata per validazione incrociata (ms)
_DURATION_TOLERANCE_MS = 5_000   # ±5 secondi


class VersionGuard:
    """
    Validatore incrociato tra risultati MB e iTunes.

    Tutti i metodi sono puri (no side-effect su song/meta), restituiscono bool
    o tuple diagnostiche, così il pipeline decide cosa fare.
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.log = logger or logging.getLogger(__name__)

    # ── API principale ────────────────────────────────────────────────────────

    def itunes_consistent_with_mb(
        self,
        itunes: ITunesResult,
        mb: MBResult,
        original_title: str,
        original_duration_ms: Optional[int] = None,
    ) -> bool:
        """
        True se il risultato iTunes è coerente con il contesto MB e con il
        titolo originale. False → il pipeline NON deve sovrascrivere
        track_number/disc_number con i valori iTunes.

        Controlla:
        1. Se il titolo originale ha tag remix, il candidato iTunes deve averlo.
        2. Se MB ha confidenza alta e iTunes ha trovato un album diverso,
           la durata deve essere compatibile.
        3. Se MB ha trovato per ISRC, iTunes deve avere lo stesso ISRC E la
           stessa "edizione" di release (single/EP/album coerenti), oppure
           titolo + artista molto simili come fallback diagnostico.
        """
        if not itunes.found:
            return False

        # --- Controllo 1: coerenza tag versione ---
        if not self._version_tags_compatible(original_title, itunes):
            self.log.debug(
                "[VersionGuard] FAIL: tag versione incompatibili tra titolo originale "
                f"'{original_title}' e iTunes '{itunes.data.get('title', '')}'"
            )
            return False

        # --- Controllo 2: coerenza durata se MB ha confidenza alta ---
        if mb.found and mb.confidence >= MatchConfidence.GOOD:
            if not self._duration_compatible(original_duration_ms, itunes.itunes_duration_ms):
                self.log.debug(
                    f"[VersionGuard] FAIL: durata incompatibile "
                    f"(originale={original_duration_ms}ms, iTunes={itunes.itunes_duration_ms}ms)"
                )
                return False

        # --- Controllo 3: se MB match per ISRC, iTunes deve essere coerente ---
        if mb.confidence == MatchConfidence.ISRC_EXACT and not itunes.matched_by_isrc:
            if not self._title_artist_compatible(mb, itunes):
                self.log.debug(
                    "[VersionGuard] FAIL: MB era ISRC-exact ma iTunes ha titolo/artista diversi"
                )
                return False

        # --- Controllo 4 (NUOVO): ISRC identico ma edizione di release diversa.
        # Stesso ISRC non garantisce stessa release: un brano può essere
        # single su un provider e già incluso in un album sull'altro.
        # In questo caso i campi di release (track_number/disc_number/album)
        # NON sono affidabili da iTunes anche se l'ISRC "matcha".
        itunes_isrc = (itunes.data.get("isrc") or "").strip()
        mb_isrc = mb.isrc.strip() if mb.found else ""
        if itunes_isrc and mb_isrc:
            safe = isrc_match_is_safe(
                itunes_isrc, mb_isrc, itunes.release_edition, mb.release_edition
            )
            if not safe:
                self.log.debug(
                    "[VersionGuard] FAIL: ISRC identico ma release edition incompatibile "
                    f"(iTunes={itunes.release_edition.describe() if itunes.release_edition else 'n/a'}, "
                    f"MB={mb.release_edition.describe() if mb.release_edition else 'n/a'})"
                )
                return False

        return True

    def safe_overwrite_fields(
        self,
        itunes: ITunesResult,
        mb: MBResult,
        original_title: str,
        original_duration_ms: Optional[int] = None,
    ) -> set[str]:
        """
        Restituisce l'insieme dei campi che possono essere sovrascritti
        incondizionatamente da iTunes.

        - cover_url, explicit: sempre sovrascrivibili (dati estetici/booleani)
        - track_number, disc_number: solo se iTunes è consistente con MB
          (incluso il check di edizione release sull'ISRC)
        - title: solo se il titolo iTunes è una versione più completa dello
          STESSO brano (vedi `_itunes_title_is_richer_same_track`) — es. il
          seed da YouTube ha "Brothers (Remix)" ma iTunes ha correttamente
          "Brothers (feat. Lil Durk) [Remix]". Senza questo, SongMeta.apply
          usa set_if_empty per il titolo: poiché il seed lo aveva già
          popolato (non vuoto), il titolo più informativo di iTunes veniva
          sempre scartato anche quando il match era certo e corretto.
        """
        always_safe = {"cover_url", "explicit"}

        if itunes.found and self._itunes_title_is_richer_same_track(original_title, itunes):
            # NOTA: solo "title" qui. "sort_title" non è una chiave presente
            # nel dict di MetaMapper.from_itunes, quindi aggiungerla a
            # overwrite_keys non avrebbe effetto: va rigenerata dal
            # chiamante (_apply_itunes) quando il titolo cambia davvero,
            # non gestita come campo passivo da questo guard.
            always_safe = always_safe | {"title"}

        if self.itunes_consistent_with_mb(itunes, mb, original_title, original_duration_ms):
            return always_safe | {"track_number", "disc_number"}

        self.log.debug(
            "[VersionGuard] track_number/disc_number NON sovrascritti: "
            "iTunes non è consistente con MB/titolo originale"
        )
        return always_safe

    def _itunes_title_is_richer_same_track(self, original_title: str, itunes: ITunesResult) -> bool:
        """
        True se il titolo iTunes è quasi certamente lo stesso brano del
        titolo originale, ma con informazioni aggiuntive (featuring, tag di
        versione) che il titolo originale non ha — quindi va promosso a
        sostituire il seed.

        Confronta sul titolo "core" (senza alcun blocco tra parentesi/
        brackets): se i core coincidono ma il titolo iTunes è più lungo
        (contiene qualcosa in più tra parentesi, tipicamente un featuring),
        è sicuro sovrascrivere. Se i core sono diversi, non è lo stesso
        brano e non si sovrascrive: il titolo originale resta quello noto.
        """
        itunes_title = (itunes.data.get("title") or "").strip()
        if not itunes_title or itunes_title == original_title:
            return False

        core_original = TextCleaner.normalize(strip_parenthetical(original_title))
        core_itunes   = TextCleaner.normalize(strip_parenthetical(itunes_title))

        if not core_original or not core_itunes:
            return False
        if TextCleaner.title_similarity(core_original, core_itunes) < 0.92:
            return False

        # Il titolo iTunes deve essere strettamente più ricco (più lungo),
        # altrimenti non c'è motivo di sovrascrivere un titolo equivalente.
        return len(itunes_title) > len(original_title)

    def title_requires_remix_in_candidate(self, title: str) -> bool:
        """True se il titolo contiene un tag remix esplicito."""
        return bool(_VERSION_TAG_RE.search(title))

    def album_is_deluxe(self, album_title: str) -> bool:
        """True se il titolo album indica un'edizione deluxe/expanded/ecc."""
        return bool(_DELUXE_TAG_RE.search(album_title))

    # ── helpers privati ───────────────────────────────────────────────────────

    def _version_tags_compatible(self, original_title: str, itunes: ITunesResult) -> bool:
        """
        Se il titolo originale ha un tag versione (es. "(Remix)"), il candidato
        iTunes deve averlo. Il contrario NON vale: un candidato con tag in più
        non è penalizzato (es. cerca "Leaked", trova "Leaked (Explicit)" → ok).
        """
        if not self.title_requires_remix_in_candidate(original_title):
            return True   # nessun tag obbligatorio

        itunes_title = itunes.data.get("title", "") or ""
        has_tag = bool(_VERSION_TAG_RE.search(itunes_title))

        if not has_tag:
            # Fallback: controlla anche collectionName (alcuni remix sono su album dedicati)
            coll = itunes.itunes_album or ""
            has_tag = bool(_VERSION_TAG_RE.search(coll))

        return has_tag

    def _duration_compatible(
        self,
        original_ms: Optional[int],
        itunes_ms: Optional[int],
    ) -> bool:
        """True se le durate sono entrambe assenti o entro la tolleranza."""
        if original_ms is None or itunes_ms is None:
            return True   # dati mancanti → non possiamo falsificare
        return abs(original_ms - itunes_ms) <= _DURATION_TOLERANCE_MS

    def _title_artist_compatible(self, mb: MBResult, itunes: ITunesResult) -> bool:
        """
        Confronto titolo+artista tra MB e iTunes tramite TextCleaner.
        Usato quando MB aveva match ISRC ma iTunes no.
        """
        mb_title  = TextCleaner.clean_text(mb.mb_title, field_type="title")
        it_title  = TextCleaner.clean_text(
            itunes.data.get("title", ""), field_type="title"
        )
        title_sim = TextCleaner.title_similarity(mb_title, it_title)

        if title_sim < 0.75:
            return False

        mb_artist = TextCleaner.clean_text(
            (mb.recording or {}).get("artist_cleaned", ""), field_type="artist"
        )
        it_artist = TextCleaner.clean_text(
            itunes.data.get("artist", ""), field_type="artist"
        )
        artist_sim = TextCleaner.title_similarity(mb_artist, it_artist)

        return artist_sim >= 0.60