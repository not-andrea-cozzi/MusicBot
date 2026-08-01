from __future__ import annotations

import logging
from typing import Dict, Optional

from Algorithm.BestMatch import TrackMatcher
from Algorithm.TextCleaner import TextCleaner
from Model.Song import Song
from Pipeline.PipelineResult import MBResult, MatchConfidence
from Providers.MusicBrainzApi import MusicBrainzApiRequstor
from Utils.MusicPatterns import MusicPatterns


class MusicBrainzResolver:

    _DELUXE_TAG_RE = MusicPatterns.DELUXE_TAG_RE

    def __init__(self, mb: MusicBrainzApiRequstor, matcher: TrackMatcher, logger: Optional[logging.Logger] = None) -> None:
        self.mb = mb
        self.matcher = matcher
        self.log = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def resolve(self, song: Song) -> MBResult:
        raw_title = song.meta.title.strip()
        raw_artist = song.meta.artist.strip()
        raw_album = song.meta.album.strip()
        isrc_hint = song.meta.isrc.strip()
        duration = song.meta.duration_ms

        clean_artist = TextCleaner.clean_text(raw_artist, field_type="artist")
        clean_title = TextCleaner.clean_text(raw_title, artist=clean_artist, field_type="title")
        clean_album = TextCleaner.normalize(raw_album)
        title_has_remix = self._has_version_tag(raw_title)

        best_score, best_rec_raw, isrc_fetch_trusted = await self._mb_isrc_attempt(
            isrc_hint, raw_title, title_has_remix, clean_title, clean_artist, clean_album, duration
        )

        if not isrc_fetch_trusted:
            best_score, best_rec_raw = await self._mb_text_search(
                raw_title, raw_artist, raw_album, title_has_remix,
                clean_title, clean_artist, clean_album, duration, isrc_hint,
                best_score, best_rec_raw,
            )

        if not best_rec_raw or best_score < 0.35:
            return MBResult()

        return await self._build_mb_result(
            best_rec_raw, best_score, isrc_hint, duration, title_has_remix, clean_album, raw_title, clean_title,
            isrc_fetch_trusted,
        )

    # ------------------------------------------------------------------
    # ISRC attempt
    # ------------------------------------------------------------------
    async def _mb_isrc_attempt(
        self, isrc_hint, raw_title, title_has_remix, clean_title, clean_artist, clean_album, duration,
    ) -> tuple[float, Optional[Dict], bool]:
        if not isrc_hint:
            return -1.0, None, False

        rec = await self.mb.fetch_by_isrc(isrc_hint)
        if not rec:
            return -1.0, None, False

        if not self._isrc_fetch_is_trustworthy(rec, raw_title, title_has_remix):
            self.log.debug(
                f"[MBResolver] ISRC hint {isrc_hint!r} scartato: titolo MB "
                f"{self.mb._recording_title(rec)!r} incoerente col tag versione."
            )
            return -1.0, None, False

        score = self.matcher.score_candidate(
            title=clean_title, artist=clean_artist, album_hint=clean_album,
            duration_ms=duration, isrc=isrc_hint, candidate=self.mb._recording_to_candidate(rec),
        )
        if score:
            return score, rec, True
        return -1.0, None, False

    def _isrc_fetch_is_trustworthy(self, rec: Dict, original_title: str, title_has_remix: bool) -> bool:
        if not title_has_remix:
            return True
        return self._has_version_tag(self.mb._recording_title(rec))

    # ------------------------------------------------------------------
    # Text search
    # ------------------------------------------------------------------
    async def _mb_text_search(
        self, raw_title, raw_artist, raw_album, title_has_remix,
        clean_title, clean_artist, clean_album, duration, isrc_hint,
        best_score, best_rec_raw,
    ) -> tuple[float, Optional[Dict]]:
        query = self.mb._build_query(raw_title, raw_artist, raw_album)
        for rec in await self.mb._search_recordings(query):
            score = self.matcher.score_candidate(
                title=clean_title, artist=clean_artist, album_hint=clean_album,
                duration_ms=duration, isrc=isrc_hint, candidate=self.mb._recording_to_candidate(rec),
            )
            if score and score > best_score:
                best_score, best_rec_raw = score, rec

        best_title_now = self.mb._recording_title(best_rec_raw) if best_rec_raw else ""
        if title_has_remix and not self._has_version_tag(best_title_now):
            tagged_query = self.mb._build_query(raw_title, raw_artist, raw_album, include_version_tag=True)
            if tagged_query != query:
                for rec in await self.mb._search_recordings(tagged_query):
                    cand_title = self.mb._recording_title(rec)
                    if not self._has_version_tag(cand_title):
                        continue
                    score = self.matcher.score_candidate(
                        title=clean_title, artist=clean_artist, album_hint=clean_album,
                        duration_ms=duration, isrc=isrc_hint, candidate=self.mb._recording_to_candidate(rec),
                    )
                    if score and score > best_score:
                        best_score, best_rec_raw = score, rec
                        self.log.debug(f"[MBResolver] fallback tag versione riuscito: '{cand_title}'")

        return best_score, best_rec_raw

    # ------------------------------------------------------------------
    # Build final MBResult
    # ------------------------------------------------------------------
    async def _build_mb_result(
        self, best_rec_raw, best_score, isrc_hint, duration, title_has_remix,
        clean_album, raw_title, clean_title, isrc_fetch_trusted,
    ) -> MBResult:
        recording = await self.mb.fetch_recording_by_id(
            best_rec_raw["id"], inc_params="releases+media+artist-credits+isrcs+release-groups+tags+genres",
        )
        if not recording:
            return MBResult()

        final_isrc = (recording.get("isrcs") or [""])[0] or (best_rec_raw.get("isrcs") or [""])[0]
        confidence = self._mb_confidence(best_score, final_isrc, isrc_hint, duration, recording.get("length"), isrc_fetch_trusted)
        releases = recording.get("releases", [])
        best_release = self._pick_best_release(releases, clean_album, raw_title)
        album_score = 0.0

        if clean_album and best_release:
            album_score = TextCleaner.album_edition_similarity(
                clean_album, TextCleaner.clean_text(best_release.get("title", ""), field_type="album"),
            )

        release_edition = (
            self.mb.edition_for_release(best_release, title_norm=TextCleaner.normalize(clean_title))
            if best_release else None
        )

        return MBResult(
            recording=recording, album=best_release, track_score=best_score, album_score=album_score,
            confidence=confidence, isrc=final_isrc,
            album_is_deluxe=bool(self._DELUXE_TAG_RE.search((best_release or {}).get("title", ""))),
            title_has_remix=title_has_remix, release_edition=release_edition,
        )

    def _pick_best_release(self, releases: list, clean_album: str, original_title: str) -> Dict:
        if not releases:
            return {}
        wants_alt = MusicPatterns.is_alt_version(original_title)
        candidates = releases if wants_alt else [
            r for r in releases if not MusicPatterns.is_alt_version(r.get("title", ""))
        ]
        pool = candidates or releases
        if not clean_album:
            return pool[0]
        best_release, best_score = pool[0], -1.0
        for r in pool:
            sim = TextCleaner.album_edition_similarity(
                clean_album, TextCleaner.clean_text(r.get("title", ""), field_type="album"),
            )
            if sim > best_score:
                best_score, best_release = sim, r
        return best_release

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------
    @staticmethod
    def _mb_confidence(score, final_isrc, isrc_hint, duration_ms, cand_ms, isrc_fetch_trusted: bool = True) -> MatchConfidence:
        if isrc_fetch_trusted and final_isrc and isrc_hint and final_isrc.upper() == isrc_hint.upper():
            return MatchConfidence.ISRC_EXACT
        if score >= 0.90:
            return MatchConfidence.HIGH
        duration_ok = abs(duration_ms - cand_ms) <= MusicPatterns.DURATION_TOLERANCE_MS if duration_ms and cand_ms else True
        if score >= 0.70 and duration_ok:
            return MatchConfidence.GOOD
        if score >= 0.55:
            return MatchConfidence.LOW
        return MatchConfidence.NONE

    @staticmethod
    def _has_version_tag(text: str) -> bool:
        return bool(MusicPatterns.VERSION_TAG_RE.search(text or ""))