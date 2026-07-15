from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from Algorithm.RegexToken import FakeAlbumSuffix
from Algorithm.TextCleaner import TextCleaner
from Helpers.MusicBrainzHelper import MusicBrainzHelper
from Helpers.MetaMapper import MetaMapper
from Model.Song import Song
from Pipeline.LocalDbMatcher import LocalDbMatcher
from Pipeline.PipelineContext import PipelineContext
from Pipeline.PipelineResult import ITunesResult, MBResult, MatchConfidence
from Pipeline.ReleaseEdition import ReleaseEdition
from Pipeline.VersionGuard import VersionGuard
from Providers.DeezerProvider import DeezerProvider
from Providers.ItunesProvider import ITunesProvider
from Providers.MusicBrainzApi import MusicBrainzApiRequstor
from Algorithm.BestMatch import TrackMatcher, strip_parenthetical
from Providers.SpotifyProvider import SpotifyProvider
from Utils.MusicPatterns import MusicPatterns


class MetadataPipeline:

    _VERSION_TAG_RE = MusicPatterns.VERSION_TAG_RE
    _DELUXE_TAG_RE = MusicPatterns.DELUXE_TAG_RE

    def __init__(
        self,
        itunes: ITunesProvider,
        mb: MusicBrainzApiRequstor,
        deezer: DeezerProvider,
        logger: Optional[logging.Logger] = None,
        spotify: Optional[SpotifyProvider] = None,
        use_mb: bool = True,
        use_deezer_recording: bool = True,
        use_spotify: bool = False,
        always_fallback_cover: bool = False,
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        self.itunes = itunes
        self.mb = mb
        self.deezer = deezer
        self.spotify = spotify
        self.use_mb = use_mb
        self.use_deezer_recording = use_deezer_recording
        self.use_spotify = use_spotify
        self.always_fallback_cover = always_fallback_cover
        self.log = logger or logging.getLogger(__name__)
        self.matcher = TrackMatcher(min_score=MusicPatterns.MATCHER_MIN_SCORE)
        self.mb.set_matcher(self.matcher)
        self.db_session = db_session
        self.db_matcher = LocalDbMatcher(db_session, self.matcher, self.log) if db_session is not None else None
        self.guard = VersionGuard(logger=self.log)

    # ── Entry point ──────────────────────────────────────────────────────

    async def run(self, song: Song) -> Song:
        song.mark_tagging()
        self.log.info(f"[Pipeline] '{song.meta.title}' - '{song.meta.artist}'")

        ctx = PipelineContext.start(song)

        await self._phase_spotify(ctx)
        await self._phase_recording_match(ctx)

        if await self._phase_local_db(ctx):
            return self._finalize_from_db(ctx)

        await self._phase_deezer_isrc(ctx)
        if ctx.deezer_isrc_result and not ctx.db_hit:
            if await self._phase_local_db(ctx, force=True):
                return self._finalize_from_db(ctx)

        await self._phase_itunes(ctx)
        await self._phase_artist_collection(ctx)
        await self._phase_deezer_fallback(ctx)

        self._finalize_compilation(ctx.song)
        return ctx.song

    # ── Fase Spotify ─────────────────────────────────────────────────────

    async def _phase_spotify(self, ctx: PipelineContext) -> None:
        if not (self.use_spotify and self.spotify and self.spotify.is_active):
            return
        song = ctx.song
        title_has_remix = self._has_version_tag(song.meta.title)

        sp_track = await self._safe_call(
            self.spotify.search, "Spotify",
            title=song.meta.title, artist=song.meta.artist, album=song.meta.album,
            duration_ms=song.meta.duration_ms, isrc=song.meta.isrc,
        )

        if title_has_remix:
            sp_track = await self._spotify_retry_with_tag(song, sp_track)

        if not sp_track:
            self.log.debug(f"[Pipeline][Spotify] miss: '{ctx.original_title}'")
            return

        sp_mapped = self.spotify.map_to_meta(sp_track)
        ctx.spotify_mapped = sp_mapped
        ctx.spotify_isrc = sp_mapped.get("isrc", "")
        self.log.info(f"[Pipeline][Spotify] '{sp_mapped.get('title')}' ISRC={ctx.spotify_isrc!r}")
        song.meta.apply(sp_mapped, overwrite_keys={"artist_collection", "artist"})

    async def _spotify_retry_with_tag(self, song: Song, sp_track: Optional[dict]) -> Optional[dict]:
        has_tag = bool(sp_track) and self._has_version_tag((sp_track or {}).get("name", ""))
        if sp_track and has_tag:
            return sp_track

        retry = None
        if hasattr(self.spotify, "search_allow_version_tag"):
            retry = await self._safe_call(
                self.spotify.search_allow_version_tag, "Spotify retry-tag",
                title=song.meta.title, artist=song.meta.artist, album=song.meta.album,
                duration_ms=song.meta.duration_ms, isrc="",
            )

        if retry and self._has_version_tag(retry.get("name", "")):
            self.log.debug(f"[Pipeline][Spotify] retry tag riuscito: '{retry.get('name')}'")
            return retry
        if sp_track and not has_tag:
            self.log.debug(f"[Pipeline][Spotify] match senza tag versione richiesto, scartato.")
            return None
        return sp_track

    # ── Fase recording match: Deezer primario, MB fallback ──────────────

    async def _phase_recording_match(self, ctx: PipelineContext) -> None:
        if self.use_deezer_recording:
            await self._resolve_deezer_recording(ctx)

        if not ctx.mb_result.found and self.use_mb:
            await self._resolve_musicbrainz(ctx)

    async def _resolve_deezer_recording(self, ctx: PipelineContext) -> None:
        song = ctx.song
        raw_title = song.meta.title.strip()
        raw_artist = song.meta.artist.strip()
        raw_album = song.meta.album.strip()
        isrc_hint = song.meta.isrc.strip()

        raw = await self._safe_call(
            self.deezer.search_recording, "Deezer-Recording",
            title=raw_title, artist=raw_artist, album_hint=raw_album,
            duration_ms=song.meta.duration_ms, isrc=isrc_hint, min_score=0.5,
        ) or {}

        if not raw:
            self.log.debug(f"[Pipeline][Deezer-Recording] miss: '{raw_title}'")
            return

        found_isrc = raw.get("isrc", "")
        matched_by_isrc = bool(isrc_hint and found_isrc and found_isrc.upper() == isrc_hint.upper())
        title_sim = TextCleaner.title_similarity(
            TextCleaner.normalize(raw_title), TextCleaner.normalize(raw.get("title", ""))
        )
        score = 1.0 if matched_by_isrc else max(title_sim, 0.5)
        confidence = self._confidence_from_score(score, is_isrc_exact=matched_by_isrc)

        edition = ReleaseEdition.from_deezer_kind(
            raw.get("_release_edition_kind", "unknown"),
            title_norm=TextCleaner.normalize(raw_title),
        )

        ctx.mb_result = MBResult(
               recording={
                    "title": raw.get("title", ""),
                    "isrcs": [found_isrc] if found_isrc else [],
                    "artist_cleaned": TextCleaner.clean_text(raw.get("artist", ""), field_type="artist"),
                },
            album={"title": raw["album"]} if raw.get("album") else None,
            track_score=score,
            confidence=confidence,
            isrc=found_isrc,
            title_has_remix=self._has_version_tag(raw_title),
            release_edition=edition,
        )

        if found_isrc:
            song.meta.set_if_empty("isrc", found_isrc)

        overwrite = {"cover_url", "genre"}
        if not raw.get("_alt_version_rejected"):
            overwrite.add("year")
        song.meta.apply(raw, overwrite_keys=overwrite)

        ctx.mb_track_disc = {k: raw[k] for k in ("track_number", "disc_number") if raw.get(k)}

        self.log.info(
            f"[Pipeline][Deezer-Recording] '{raw.get('title')}' "
            f"confidence={confidence.name} ISRC={found_isrc!r}"
        )

    async def _resolve_musicbrainz(self, ctx: PipelineContext) -> None:
        mb_result = await self._resolve_mb(ctx.song)
        ctx.mb_result = mb_result
        song = ctx.song

        if mb_result.found:
            await self._apply_mb_country(mb_result.recording)
            song.meta.set_if_empty("isrc", mb_result.isrc)
            edition_desc = mb_result.release_edition.describe() if mb_result.release_edition else "n/a"
            self.log.debug(f"[Pipeline] MB(fallback) confidence={mb_result.confidence.name} edition={edition_desc}")
        else:
            self.log.warning(f"[Pipeline] MB(fallback) miss: '{ctx.original_title}'")

        ctx.mb_track_disc = self._resolve_mb_track_disc(song, mb_result)

    async def _resolve_mb(self, song: Song) -> MBResult:
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
                f"[Pipeline] ISRC hint {isrc_hint!r} scartato: titolo MB "
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
                        self.log.debug(f"[Pipeline][MB] fallback tag versione riuscito: '{cand_title}'")

        return best_score, best_rec_raw

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

    def _isrc_fetch_is_trustworthy(self, rec: Dict, original_title: str, title_has_remix: bool) -> bool:
        if not title_has_remix:
            return True
        return self._has_version_tag(self.mb._recording_title(rec))

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

    # ── Fase DB locale ────────────────────────────────────────────────────

    async def _phase_local_db(self, ctx: PipelineContext, force: bool = False) -> bool:
        if not self.db_session or (ctx.db_hit and not force):
            return bool(ctx.db_hit_readonly and ctx.db_hit)

        isrc = ctx.search_isrc
        song = ctx.song
        db_hit = await self._safe_call(self._lookup_local_db, "DB", song, isrc) or {}

        if not db_hit:
            return False

        ctx.db_hit = db_hit
        ctx.db_hit_readonly = True
        self.log.info("[Pipeline][DB] Hit locale → read-only, salto Deezer + iTunes.")
        return True

    async def _lookup_local_db(self, song: Song, isrc: str) -> Dict[str, Any]:
        """
        Cerca la song nel DB locale delegando interamente a LocalDbMatcher:
        priorità a titolo+artista+album (segnale diretto, meno soggetto a
        inquinamento storico del DB rispetto al solo ISRC), ISRC come
        fallback. La distinzione single/album è gestita internamente da
        LocalDbMatcher sui dati REALI degli album candidati.
        """
        if not self.db_matcher:
            return {}

        track = await self.db_matcher.find(
            title=song.meta.title, artist=song.meta.artist, album_hint=song.meta.album,
            duration_ms=song.meta.duration_ms, isrc=isrc,
        )
        if not track:
            return {}

        self.log.debug(f"[Pipeline][DB] Match: '{track.track_name}' (collection_id={track.collection_id})")
        return await self.db_matcher.to_meta(track)

    def _finalize_from_db(self, ctx: PipelineContext) -> Song:
        song = ctx.song
        song.meta.apply(ctx.db_hit, overwrite_keys=set(ctx.db_hit.keys()))
        if ctx.mb_track_disc:
            db_has_track = bool(ctx.db_hit.get("track_number"))
            db_has_disc = bool(ctx.db_hit.get("disc_number"))
            filtered_mb_track_disc = {
                k: v for k, v in ctx.mb_track_disc.items()
                if not (k == "track_number" and db_has_track)
                and not (k == "disc_number" and db_has_disc)
            }
            if filtered_mb_track_disc:
                self._apply_mb_track_disc(song, filtered_mb_track_disc)
            elif ctx.mb_track_disc:
                self.log.debug(
                    "[Pipeline] mb_track_disc scartato: DB-hit ha già "
                    "track_number/disc_number autoritativi."
                )
        ctx.accurate_artists = (ctx.spotify_mapped or {}).get("artist_collection", "")
        self._finalize_artist_collection(
            song, ctx.accurate_artists, itunes_artist_raw=ctx.db_hit.get("artist", ""),
            raw_title=song.raw.get("title", ""),
        )
        self._finalize_compilation(song)
        return song

    # ── Fase Deezer ISRC ─────────────────────────────────────────────────

    async def _phase_deezer_isrc(self, ctx: PipelineContext) -> None:
        isrc = ctx.search_isrc
        if not isrc:
            await self._step_deezer(ctx.song, ctx.original_title, itunes_found=False, mb_found=ctx.mb_result.found)
            return

        raw = await self._safe_call(self.deezer.get_by_isrc, "Deezer-ISRC", isrc) or {}
        if not raw:
            self.log.debug(f"[Pipeline][Deezer-ISRC] ISRC {isrc} non trovato")
            return

        mapped = MetaMapper.from_deezer_isrc(raw, logger=self.log)
        ctx.deezer_isrc_result = mapped
        self.log.debug(f"[Pipeline][Deezer-ISRC] '{mapped.get('title')}' genre={mapped.get('genre')!r}")

        title_has_remix = self._has_version_tag(ctx.song.meta.title)
        deezer_title_ok = not title_has_remix or self._has_version_tag(mapped.get("title", ""))

        overwrite = {"cover_url", "explicit", "genre", "year"}
        if deezer_title_ok:
            overwrite |= {"track_number", "disc_number"}
        else:
            self.log.debug(f"[Pipeline][Deezer-ISRC] track/disc non applicati: titolo incoerente.")

        ctx.song.meta.apply(mapped, overwrite_keys=overwrite)
        self.log.info("[Pipeline][Deezer-ISRC] Applicato.")

    # ── Fase iTunes ──────────────────────────────────────────────────────

    async def _phase_itunes(self, ctx: PipelineContext) -> None:
        song = ctx.song
        mb_result = ctx.mb_result
        search_album = self._resolve_search_album(song, mb_result)
        itunes_min = 0.70 if mb_result.confidence == MatchConfidence.LOW else MusicPatterns.MATCHER_MIN_SCORE

        itunes_result = await self._search_itunes(
            song, override_title=ctx.original_title or mb_result.mb_title,
            override_album=search_album, override_isrc=ctx.search_isrc, min_score=itunes_min,
        )
        ctx.itunes_result = itunes_result

        if itunes_result.found:
            self._apply_itunes(song, itunes_result, mb_result, ctx.mb_track_disc, ctx.original_title, ctx.original_duration_ms)
            await self._fix_bad_year(song, mb_result)
            if ctx.search_isrc and itunes_result.data.get("itunes_track_id"):
                await self.itunes.persist_track_isrc(int(itunes_result.data["itunes_track_id"]), ctx.search_isrc)
        else:
            self.log.warning(f"[Pipeline] iTunes miss: '{ctx.original_title}'")
            if not ctx.deezer_isrc_result and ctx.spotify_mapped:
                song.meta.apply(ctx.spotify_mapped)
            elif not ctx.deezer_isrc_result and mb_result.found:
                self._apply_mb_fallback(song, mb_result)

    async def _search_itunes(
        self, song: Song, override_title: str = "", override_album: str = "",
        override_isrc: str = "", min_score: float = MusicPatterns.MATCHER_MIN_SCORE,
    ) -> ITunesResult:
        title = (override_title or song.meta.title).strip()
        artist = song.meta.artist.strip()
        album = (override_album or song.meta.album).strip()
        isrc = (override_isrc or song.meta.isrc).strip()
        duration = song.meta.duration_ms

        if not title:
            return ITunesResult()

        raw = await self._safe_call(
            self.itunes.search, "iTunes",
            title=title, song=song, artist=artist, hint_album=album, duration_ms=duration, min_score=min_score,
        )
        if not raw:
            return ITunesResult()

        matched_by_isrc = bool(isrc and raw.get("isrc", "").upper() == isrc.upper())
        edition = raw.pop("_release_edition", None)
        return self._build_itunes_result(raw, matched_by_isrc=matched_by_isrc, title=title, artist=artist, edition=edition)

    def _build_itunes_result(self, raw, matched_by_isrc=False, title="", artist="", edition: Optional[ReleaseEdition] = None) -> ITunesResult:
        confidence = MatchConfidence.ISRC_EXACT if matched_by_isrc else self._itunes_confidence(raw, title, artist)
        return ITunesResult(
            data=raw, confidence=confidence, matched_by_isrc=matched_by_isrc,
            itunes_track_number=raw.get("track_number"), itunes_disc_number=raw.get("disc_number"),
            itunes_duration_ms=raw.get("track_time_ms") or raw.get("duration_ms"),
            itunes_album=raw.get("album", ""), release_edition=edition,
        )

    # ── Fase artist_collection ───────────────────────────────────────────

    async def _phase_artist_collection(self, ctx: PipelineContext) -> None:
        song = ctx.song
        accurate_artists = await self._resolve_accurate_artists(song, ctx.spotify_mapped, ctx.deezer_isrc_result, ctx.original_title)
        ctx.accurate_artists = accurate_artists
        itunes_artist_raw = ctx.itunes_result.data.get("artist", "") if ctx.itunes_result.found else ""
        self._finalize_artist_collection(song, accurate_artists, itunes_artist_raw, raw_title=song.raw.get("title", ""))

    # ── Fase Deezer fallback ─────────────────────────────────────────────

    async def _phase_deezer_fallback(self, ctx: PipelineContext) -> None:
        await self._step_deezer(ctx.song, ctx.original_title, ctx.itunes_result.found, ctx.mb_result.found or bool(ctx.deezer_isrc_result))
        self._finalize_compilation(ctx.song)

    async def _step_deezer(self, song: Song, original_title: str, itunes_found: bool, mb_found: bool) -> None:
        m = song.meta
        if not itunes_found and not mb_found:
            self.log.warning(f"[Pipeline][Deezer] Full fallback: '{original_title}'")
            raw = await self._safe_call(self.deezer.get_full_metadata, "Deezer-full", title=original_title, artist=m.artist, album=m.album) or {}
            if raw:
                song.meta.apply(MetaMapper.from_deezer(raw, logger=self.log))

        if self.always_fallback_cover or "mzstatic.com/image" not in m.cover_url:
            url = await self._safe_call(self.deezer.get_cover_url, "Deezer-cover", title=m.title, artist=m.artist, album=m.album)
            if url:
                m.cover_url = url

        await self._deezer_fill_missing(song)

        if not m.year:
            raw_year = song.raw.get("year", "")
            if raw_year:
                m.year = str(raw_year)

    async def _deezer_fill_missing(self, song: Song) -> None:
        m = song.meta
        partial: Dict[str, Any] = {}
        needs_track_disc = m.track_number == 0 or m.disc_number == 0
        needs_genre = not m.genre

        async def _fetch_track_disc():
            if not needs_track_disc:
                return
            dt, dd = await self._safe_call(self.deezer.get_track_and_disc, "Deezer-track/disc", title=m.title, artist=m.artist, album=m.album) or (0, 0)
            if dt > 0 and m.track_number == 0:
                partial["track_number"] = dt
            if dd > 0 and m.disc_number == 0:
                partial["disc_number"] = dd

        async def _fetch_genre():
            if not needs_genre:
                return
            g = await self._safe_call(self.deezer.get_genre, "Deezer-genre", title=m.title, artist=m.artist)
            if g:
                partial["genre"] = g

        if needs_track_disc or needs_genre:
            await asyncio.gather(_fetch_track_disc(), _fetch_genre())
        if partial:
            song.meta.apply(partial)

    # ── Apply helpers ────────────────────────────────────────────────────

    def _apply_itunes(self, song, itunes, mb, mb_track_disc, original_title, original_duration) -> None:
        overwrite = self.guard.safe_overwrite_fields(itunes, mb, original_title, original_duration)
        itunes_data = dict(itunes.data)
        for key in ("track_number", "disc_number"):
            if mb_track_disc.get(key):
                itunes_data.pop(key, None)
                overwrite.discard(key)

        title_will_change = "title" in overwrite and itunes_data.get("title")
        song.meta.apply(itunes_data, overwrite_keys=overwrite)

        if title_will_change:
            song.meta.sort_title = Song.build_sort_name(song.meta.title)
            self.log.debug(f"[Pipeline] title arricchito da iTunes: {song.meta.title!r}")

        self.log.info("[Pipeline] iTunes applicato.")

    def _apply_mb_track_disc(self, song: Song, mb_track_disc: Dict[str, int]) -> None:
        for key in ("disc_number", "track_number"):
            val = mb_track_disc.get(key)
            if val:
                setattr(song.meta, key, val)
                self.log.debug(f"[Pipeline] {key} forzato = {val}")

    def _apply_mb_fallback(self, song: Song, mb: MBResult) -> None:
        if not mb.found or not mb.recording.get("id") and "releases" not in mb.recording:
            # MBResult sintetico da Deezer non ha struttura MB completa: skip.
            if not mb.recording or "releases" not in mb.recording:
                return
        song.meta.apply(MetaMapper.from_mb_recording(mb.recording, mb.album, logger=self.log))
        detail = mb.album or mb.recording
        song_title = TextCleaner.clean_text(song.meta.title, field_type="title")
        song_album = TextCleaner.clean_text(song.meta.album, field_type="album")
        first_media, first_track, all_tracks = MusicBrainzHelper._resolve_media_and_track(detail, song_title, song_album, self.log)
        song.meta.apply(MetaMapper.from_mb_track(first_media, first_track, all_tracks, logger=self.log))
        MusicBrainzHelper.apply_exclusive(song, mb.recording, logger=self.log)
        if not song.meta.album_artist and mb.album:
            credits = mb.album.get("artist-credit", [])
            aa = "".join(ac.get("name", "") + ac.get("joinphrase", "") for ac in credits if isinstance(ac, dict)).strip()
            if aa:
                song.meta.album_artist = aa
        if not song.meta.label and mb.album:
            label_info = mb.album.get("label-info", [])
            if label_info:
                label = label_info[0].get("label", {}).get("name", "")
                if label:
                    song.meta.label = label

    # ── Resolve helpers ──────────────────────────────────────────────────

    def _resolve_mb_track_disc(self, song: Song, mb: MBResult) -> Dict[str, int]:
        if not mb.found or mb.confidence < MatchConfidence.GOOD:
            return {}
        if mb.title_has_remix and not self._has_version_tag(mb.mb_title or ""):
            self.log.debug(f"[Pipeline] track/disc NON forzati: recording incoerente col tag versione.")
            return {}

        detail = mb.album or mb.recording
        if not detail or "media" not in detail and "releases" not in detail:
            return {}
        first_media, first_track, all_tracks = MusicBrainzHelper._resolve_media_and_track(
            detail, TextCleaner.clean_text(song.meta.title, field_type="title"),
            TextCleaner.clean_text(song.meta.album, field_type="album"), self.log,
        )
        if not first_track:
            return {}
        result: Dict[str, int] = {}
        raw_number = first_track.get("number", "")
        if raw_number:
            tn = MusicBrainzHelper.parse_track_number(raw_number, all_tracks)
            if tn:
                result["track_number"] = tn
        dn = first_media.get("position")
        if dn:
            try:
                result["disc_number"] = int(dn)
            except (ValueError, TypeError):
                pass
        if result:
            self.log.debug(f"[Pipeline] MB track/disc: {result}")
        return result

    def _resolve_search_album(self, song: Song, mb: MBResult) -> str:
        original = song.meta.album
        if mb.mb_album_title and self._DELUXE_TAG_RE.search(original):
            return original
        return mb.mb_album_title or original

    async def _resolve_accurate_artists(self, song: Song, sp_mapped: dict, deezer_isrc_result: Optional[Dict[str, Any]] = None, original_title: str = "") -> str:
        sp_artists = (sp_mapped or {}).get("artist_collection", "")
        if sp_artists:
            return sp_artists
        dz_artists = (deezer_isrc_result or {}).get("artist_collection", "")
        if dz_artists:
            return dz_artists
        if self.use_mb:
            return await self._resolve_mb_artists(song, original_title)
        return ""

    async def _resolve_mb_artists(self, song: Song, original_title: str = "") -> str:
        if not song.meta.isrc:
            return ""
        artists, _ = await self._safe_call(
            self.mb.get_accurate_artists_by_isrc, "MB-artists",
            song.meta.isrc, original_title=original_title or song.meta.title,
        ) or ("", "")
        return artists

    async def _apply_mb_country(self, recording: Dict) -> None:
        artist = next(
            (ac.get("artist", {}).get("name", "") or ac.get("name", "") for ac in recording.get("artist-credit", []) if isinstance(ac, dict)),
            "",
        )
        if not artist:
            return
        country = await self._safe_call(self.mb._resolve_artist_country, "MB-country", artist)
        target = country.upper() if country and country.upper() in MusicPatterns.ITUNES_VALID_COUNTRIES else "US"
        self.itunes.set_country_for_artist(target)

    # ── Finalizzazione ───────────────────────────────────────────────────

    def _finalize_artist_collection(self, song: Song, accurate_artists: str, itunes_artist_raw: str = "", raw_title: str = "") -> None:
        m = song.meta
        if accurate_artists:
            m.artist_collection = MusicPatterns.normalize_artist_list(accurate_artists)
            return
        if itunes_artist_raw and MusicPatterns.MULTI_ARTIST_SEP_RE.search(itunes_artist_raw):
            normalized = MusicPatterns.normalize_artist_list(itunes_artist_raw)
            if normalized.count(",") >= 1:
                m.artist_collection = normalized
                return
        title_to_search = raw_title or m.title
        feat_match = MusicPatterns.FEAT_RE.search(title_to_search)
        if feat_match:
            featuring = feat_match.group(1).strip()
            if featuring.lower() not in m.artist.lower():
                current = m.artist_collection or m.artist
                if featuring.lower() not in current.lower():
                    m.artist_collection = MusicPatterns.normalize_artist_list(f"{current}, {featuring}")
                return
        if not m.artist_collection:
            m.artist_collection = m.artist

    def _finalize_compilation(self, song: Song) -> None:
        m = song.meta
        norm_aa = TextCleaner.normalize(m.album_artist)
        norm_primary = TextCleaner.normalize(TextCleaner.primary_artist(m.artist))
        is_various = norm_aa in MusicPatterns.VARIOUS_ARTISTS
        m.compilation = is_various or bool(norm_primary and norm_aa and norm_primary != norm_aa)

    # ── Anno ─────────────────────────────────────────────────────────────

    async def _fix_bad_year(self, song: Song, mb: MBResult) -> None:
        year = str(song.meta.year).strip()
        if mb.found and mb.recording:
            for date_src in (
                mb.recording.get("first-release-date", ""),
                (mb.album or {}).get("date", ""),
                (mb.album or {}).get("first-release-date", ""),
            ):
                if date_src and len(date_src) >= 4 and date_src[:4].isdigit():
                    mb_year = int(date_src[:4])
                    current = int(year) if (year and year.isdigit()) else 9999
                    if 1900 <= mb_year <= 2100 and mb_year < current:
                        song.meta.year = str(mb_year)
                        return
                    if 1900 <= mb_year <= 2100 and not (year and year.isdigit()):
                        song.meta.year = str(mb_year)
                        return

        if not year or not year.isdigit() or not (1900 <= int(year) <= 2100):
            song.meta.year = ""
            rec_id = (mb.recording or {}).get("id") if mb.found else None
            if not rec_id:
                return
            rec = await self._safe_call(self.mb.fetch_recording_by_id, "MB-year", rec_id, inc_params="releases+release-groups")
            if not rec:
                return
            for date_src in (
                rec.get("first-release-date", ""),
                (rec.get("release-groups") or [{}])[0].get("first-release-date", ""),
                (rec.get("releases") or [{}])[0].get("date", ""),
            ):
                if date_src and len(date_src) >= 4 and date_src[:4].isdigit():
                    y = int(date_src[:4])
                    if 1900 <= y <= 2100:
                        song.meta.year = str(y)
                        return

    # ── Score helpers ────────────────────────────────────────────────────

    @staticmethod
    def _confidence_from_score(score: float, is_isrc_exact: bool = False) -> MatchConfidence:
        if is_isrc_exact:
            return MatchConfidence.ISRC_EXACT
        if score >= 0.90:
            return MatchConfidence.HIGH
        if score >= 0.70:
            return MatchConfidence.GOOD
        if score >= 0.55:
            return MatchConfidence.LOW
        return MatchConfidence.NONE

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
    def _itunes_confidence(raw, original_title, original_artist) -> MatchConfidence:
        t = TextCleaner.title_similarity(
            TextCleaner.clean_text(original_title, field_type="title"),
            TextCleaner.clean_text(raw.get("title", ""), field_type="title"),
        )
        a = TextCleaner.title_similarity(
            TextCleaner.clean_text(original_artist, field_type="artist"),
            TextCleaner.clean_text(raw.get("artist", ""), field_type="artist"),
        )
        c = 0.6 * t + 0.4 * a
        if c >= 0.90:
            return MatchConfidence.HIGH
        if c >= 0.75:
            return MatchConfidence.GOOD
        if c >= 0.55:
            return MatchConfidence.LOW
        return MatchConfidence.NONE

    @classmethod
    def _has_version_tag(cls, text: str) -> bool:
        return bool(cls._VERSION_TAG_RE.search(text or ""))

    # ── Utility: chiamata provider con try/except centralizzato ──────────

    async def _safe_call(self, fn, label: str, *args, **kwargs):
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            self.log.debug(f"[Pipeline][{label}] {exc}")
            return None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for provider in (self.mb, self.itunes, self.deezer):
            if hasattr(provider, "close"):
                try:
                    await provider.close()
                except Exception:
                    pass