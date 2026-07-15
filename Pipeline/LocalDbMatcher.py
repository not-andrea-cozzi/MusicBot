from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from Algorithm.BestMatch import TrackMatcher, strip_parenthetical
from Algorithm.RegexToken import FakeAlbumSuffix
from Algorithm.TextCleaner import TextCleaner
from Database.Model.ItunesModel import AppleMusicAlbum, AppleMusicTrack
from Helpers.MetaMapper import MetaMapper
from Pipeline.ReleaseEdition import ReleaseEdition, ReleaseKind
from Utils.MusicPatterns import MusicPatterns


@dataclass(frozen=True)
class _Query:
    title_norm: str
    artist_norm: str
    hint_norm: str
    duration_ms: Optional[int]
    isrc: str
    has_remix: bool
    expects_short_form: bool


class LocalDbMatcher:

    MAX_CANDIDATES = 3000
    MIN_KEYWORD_LEN = 3
    MAX_KEYWORDS_PER_FIELD = 3

    def __init__(
        self,
        session: AsyncSession,
        matcher: Optional[TrackMatcher] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.session = session
        self.matcher = matcher or TrackMatcher(min_score=MusicPatterns.MATCHER_MIN_SCORE)
        self.log = logger or logging.getLogger(__name__)
        self._album_cache: Dict[int, AppleMusicAlbum] = {}

    # ── Public API ───────────────────────────────────────────────────

    async def find(
        self, title: str, artist: str = "", album_hint: str = "",
        duration_ms: Optional[int] = None, isrc: str = "",
    ) -> Optional[AppleMusicTrack]:
        """Priorità a titolo+artista+album (segnale diretto); ISRC solo come fallback."""
        if not title or not title.strip():
            return None

        q = self._build_query(title, artist, album_hint, duration_ms, isrc)
        try:
            track = await self._find_by_title_artist(q)
            if track:
                return track
            if q.isrc:
                return await self._find_by_isrc(q)
        except Exception as exc:
            self.log.debug(f"[LocalDbMatcher] find() fallito: {exc}", exc_info=True)
        return None

    async def to_meta(self, track: AppleMusicTrack) -> Dict[str, Any]:
        raw = self._track_to_dict(track)
        mapped = MetaMapper.from_itunes(
            item=raw, default_title=track.track_name or "",
            default_artist=track.artist_name or "", logger=self.log,
        )
        mapped["_from_db"] = True
        return mapped

    # ── Query building ───────────────────────────────────────────────

    def _build_query(self, title, artist, album_hint, duration_ms, isrc) -> _Query:
        title_norm = TextCleaner.normalize(title)
        artist_norm = TextCleaner.normalize(TextCleaner.primary_artist(artist)) if artist else ""
        hint_norm = TextCleaner.normalize(album_hint) if album_hint else ""
        return _Query(
            title_norm=title_norm, artist_norm=artist_norm, hint_norm=hint_norm,
            duration_ms=duration_ms, isrc=(isrc or "").upper(),
            has_remix=self._has_version_tag(title),
            expects_short_form=self._expects_short_form(hint_norm, title_norm),
        )

    # ── Title/Artist path ────────────────────────────────────────────

    async def _find_by_title_artist(self, q: _Query) -> Optional[AppleMusicTrack]:
        rows = await self._candidate_rows(q.title_norm, q.artist_norm)
        if not rows:
            return None
        await self._preload_albums(rows)

        best_track, best_score = None, -1.0
        for row in rows:
            if q.has_remix and not self._has_version_tag(row.track_name or ""):
                continue
            try:
                base = self.matcher.score_candidate(
                    title=q.title_norm, artist=q.artist_norm, album_hint=q.hint_norm,
                    duration_ms=q.duration_ms, isrc="", candidate=self._as_candidate(row),
                )
                if base is None:
                    continue
                total = base + self._edition_score(row, q)
            except Exception as exc:
                self.log.debug(f"[LocalDbMatcher] scoring row {row.track_id}: {exc}")
                continue
            if total > best_score:
                best_score, best_track = total, row
        return best_track

    # ── ISRC path (fallback) ─────────────────────────────────────────

    async def _find_by_isrc(self, q: _Query) -> Optional[AppleMusicTrack]:
        stmt = select(AppleMusicTrack).where(AppleMusicTrack.isrc == q.isrc)
        rows = list((await self.session.execute(stmt)).scalars().all())
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]

        if q.has_remix:
            tagged = [r for r in rows if self._has_version_tag(r.track_name or "")]
            rows = tagged or rows

        if q.title_norm:
            scored = [(TextCleaner.title_similarity(q.title_norm, TextCleaner.normalize(r.track_name or "")), r) for r in rows]
            best_sim = max(s for s, _ in scored)
            rows = [r for s, r in scored if s >= best_sim - 0.05]
            if len(rows) == 1:
                return rows[0]

        await self._preload_albums(rows)
        return max(rows, key=lambda r: self._edition_score(r, q))

   

    async def _candidate_rows(self, title_norm: str, artist_norm: str) -> list[AppleMusicTrack]:
        title_kw = self._keywords(title_norm)
        artist_kw = self._keywords(artist_norm)
        if not title_kw and not artist_kw:
            return []

        conds = []
        if title_kw:
            conds.append(or_(*(AppleMusicTrack.track_name.ilike(f"%{w}%") for w in title_kw)))
        if artist_kw:
            conds.append(or_(*(AppleMusicTrack.artist_name.ilike(f"%{w}%") for w in artist_kw)))

        where = and_(*conds) if len(conds) > 1 else conds[0]
        stmt = select(AppleMusicTrack).where(where).limit(self.MAX_CANDIDATES)
        rows = list((await self.session.execute(stmt)).scalars().all())

        if not rows and len(conds) > 1:  # AND troppo restrittivo: allarga a OR
            stmt = select(AppleMusicTrack).where(or_(*conds)).limit(self.MAX_CANDIDATES)
            rows = list((await self.session.execute(stmt)).scalars().all())
        return rows

    @classmethod
    def _keywords(cls, norm: str) -> list[str]:
        words = {w for w in norm.split() if len(w) >= cls.MIN_KEYWORD_LEN and w not in TextCleaner._STOPWORDS}
        return sorted(words, key=len, reverse=True)[: cls.MAX_KEYWORDS_PER_FIELD]

   

    async def _preload_albums(self, rows: Iterable[AppleMusicTrack]) -> None:
        ids = {r.collection_id for r in rows if r.collection_id and r.collection_id not in self._album_cache}
        if not ids:
            return
        stmt = select(AppleMusicAlbum).where(AppleMusicAlbum.collection_id.in_(ids))
        for a in (await self.session.execute(stmt)).scalars().all():
            self._album_cache[a.collection_id] = a

   

    @staticmethod
    def _expects_short_form(hint_norm: str, title_norm: str) -> bool:
        if not hint_norm:
            return True
        if FakeAlbumSuffix.has(hint_norm):
            return True
        core_title = strip_parenthetical(title_norm).strip()
        return hint_norm in (title_norm, core_title)

    def _edition_score(self, row: AppleMusicTrack, q: _Query) -> float:
        album = self._album_cache.get(row.collection_id) if row.collection_id else None
        if not album:
            return 0.0

        if TextCleaner.normalize(album.artist_name or "") in MusicPatterns.VARIOUS_ARTISTS:
            return -0.5  # compilation "Various Artists": mai autoritativa per track/disc/album

        edition = ReleaseEdition.from_collection(
            collection_type=album.collection_type or "", collection_name=album.collection_name or "",
            track_count=album.track_count or 0, title_norm=q.title_norm,
        )
        if edition.kind is ReleaseKind.COMPILATION:
            return -0.5

        if q.expects_short_form:
            return 0.20 if edition.is_short_form else -0.15

       
        album_norm = TextCleaner.normalize(album.collection_name or "")
        sim = TextCleaner.album_edition_similarity(q.hint_norm, album_norm)
        if sim >= 0.85:
            return 0.35
        if edition.is_short_form:
            return -0.25  # in DB c'è solo il single, ma cercavamo un brano d'album
        return sim * 0.3 - 0.05


    @staticmethod
    def _has_version_tag(text: str) -> bool:
        return bool(MusicPatterns.VERSION_TAG_RE.search(text or ""))

    @staticmethod
    def _as_candidate(row: AppleMusicTrack) -> dict:
        return {
            "trackName": row.track_name or "", "artistName": row.artist_name or "",
            "collectionName": row.collection_name or "", "trackTimeMillis": row.track_time_millis,
        }

    @staticmethod
    def _track_to_dict(t: AppleMusicTrack) -> dict:
        return {
            "wrapperType": "track", "kind": "song",
            "trackId": t.track_id, "artistId": t.artist_id, "collectionId": t.collection_id,
            "artistName": t.artist_name, "collectionName": t.collection_name, "trackName": t.track_name,
            "collectionArtistName": t.collection_artist_name, "artworkUrl100": t.artwork_url,
            "trackExplicitness": t.track_explicitness, "discCount": t.disc_count,
            "discNumber": t.disc_number, "trackCount": t.track_count, "trackNumber": t.track_number,
            "trackTimeMillis": t.track_time_millis, "primaryGenreName": t.primary_genre_name,
            "isrc": t.isrc,
        }