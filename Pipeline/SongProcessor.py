# Pipeline/SongProcessor.py
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from Model.Song import Song
from Pipeline.MetadataPipeline import MetadataPipeline
from Utils.CoverManager import CoverManager
from Model.SongFileManager import SongFileManager
from Algorithm.TextCleaner import TextCleaner
from Algorithm.RegexToken import FakeAlbumSuffix
from Utils.MusicPatterns import MusicPatterns

_PRESERVE_SUFFIX_RE = re.compile(r'\((instrumental|interlude)\)', re.IGNORECASE)
_GENERIC_CHANNEL_RE = re.compile(
    r'(vevo|topic|official|channel|music|lyrics|audio|video|mix|playlist|sounds?|records?|entertainment)$',
    re.IGNORECASE,
)
_PLAYLIST_NOISE_RE = re.compile(
    r'\b(?:mix|top|chart|hits|best|playlist|compilation|radio|'
    r'official|selected|collection|favorite|favourite|musica|music|'
    r'megamix|party|dance|workout|study|relax|sleep|'
    r'greatest\s+hits|best\s+of|power\s+hits|now\s+that)\b',
    re.IGNORECASE,
)
_ALBUM_FROM_PLAYLIST_RE = re.compile(r'^Album\s*[-–]\s*(.+)$', re.IGNORECASE)
_QUOTED_ALBUM_RE = re.compile(r"['\u2018\u2019\u02bc](.+?)['\u2018\u2019\u02bc]")
_COMPILATION_WORDS = (
    "compilation", "hits", "playlist", "best of", "greatest", "power hits",
)
_VARIOUS_ARTISTS_TOKENS = ("various artists", "aa.vv.", "artisti vari")


class SongProcessor:

    def __init__(
        self,
        pipeline: MetadataPipeline,
        file_manager: SongFileManager,
        cover_manager: CoverManager,
        output_dir: str,
        logger: logging.Logger,
        cover_executor,
        cover_timeout: int = 30,
    ) -> None:
        self.pipeline       = pipeline
        self.files          = file_manager
        self.covers         = cover_manager
        self.output_dir     = output_dir
        self.logger         = logger
        self.cover_executor = cover_executor
        self.cover_timeout  = cover_timeout

    # ── public ───────────────────────────────────────────────────────────────

    def process(self, song: Song) -> None:
        try:
            asyncio.run(self._process_async(song))
        except Exception as exc:
            self.logger.error(f"[SongProcessor] {song.video_id}: {exc}", exc_info=True)
            song.mark_error(str(exc))

    # ── seed building ───────────────────────────────────────────────────────────

    def _build_seed(self, song: Song) -> dict:
        info       = song.raw
        raw_title  = info.get("title", "") or ""
        raw_artist = info.get("artist") or info.get("uploader", "") or ""

        if " - " in raw_title and not raw_artist:
            raw_artist, raw_title = raw_title.split(" - ", 1)
        else:
            raw_artist, raw_title = TextCleaner.extract_artist_from_title(raw_title, raw_artist)

        raw_title, raw_artist = TextCleaner.enrich_artist_from_title(raw_title, raw_artist)

        if raw_artist and _GENERIC_CHANNEL_RE.search(raw_artist):
            self.logger.debug(f"[Seed] Canale generico ignorato: {raw_artist!r}")
            raw_artist = ""

        preserved = ""
        if m := _PRESERVE_SUFFIX_RE.search(raw_title):
            preserved = f" ({m.group(1).capitalize()})"

        clean_title = TextCleaner.clean_title(raw_title, raw_artist)
        if preserved and preserved.lower() not in clean_title.lower():
            clean_title += preserved

        year_raw      = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")
        track_num_raw = info.get("track_number")
        track_number  = int(track_num_raw) if track_num_raw and str(track_num_raw).isdigit() else 0
        duration_sec  = info.get("duration")
        duration_ms   = int(duration_sec * 1000) if duration_sec else None

        seed = {
            "title":             clean_title,
            "artist":            TextCleaner.primary_artist(raw_artist) if raw_artist else "",
            "album":             info.get("album") or "",
            "year":              year_raw,
            "track_number":      track_number,
            "duration_ms":       duration_ms,
            "cover_url":         info.get("thumbnail") or "",
            "isrc":              info.get("isrc") or "",
            "video_id":          song.video_id,
            "_preserved_suffix": preserved,
            "_raw_artist_full":  raw_artist,
            "_playlist_title":   info.get("playlist_title", "") or "",
            "_playlist_id":      info.get("playlist_id", "") or "",
        }

        self._recover_album_from_playlist(seed, info)
        return seed

    @staticmethod
    def _recover_album_from_playlist(seed: dict, info: dict) -> None:
        playlist_title = info.get("playlist_title", "") or ""
        if not playlist_title:
            return

        if pt_match := _ALBUM_FROM_PLAYLIST_RE.match(playlist_title):
            playlist_album = pt_match.group(1).strip()
            current_album  = seed.get("album", "")
            if not current_album or (
                MusicPatterns.DELUXE_TAG_RE.search(playlist_album)
                and not MusicPatterns.DELUXE_TAG_RE.search(current_album)
            ):
                seed["album"] = playlist_album

        raw_album = seed.get("album", "")
        if raw_album and FakeAlbumSuffix.has(raw_album):
            if alb_match := _QUOTED_ALBUM_RE.search(playlist_title):
                recovered = alb_match.group(1).strip()
                if recovered and not FakeAlbumSuffix.has(recovered):
                    seed["album"] = recovered

    # ── post-process ─────────────────────────────────────────────────────────

    def _postprocess_meta(self, song: Song) -> None:
        m = song.meta
        m.clear_internals()

        primary_artist = TextCleaner.primary_artist(m.artist) if m.artist else m.artist
        m.album_artist = m.album_artist or primary_artist

        m.set_if_empty("sort_title",        Song.build_sort_name(m.title))
        m.set_if_empty("sort_artist",       Song.build_sort_name(primary_artist))
        m.set_if_empty("sort_album",        Song.build_sort_name(m.album))
        m.set_if_empty("sort_album_artist", Song.build_sort_name(m.album_artist))

        if not m.compilation:
            norm_aa    = TextCleaner.normalize(m.album_artist)
            norm_pa    = TextCleaner.normalize(primary_artist)
            is_various = norm_aa in _VARIOUS_ARTISTS_TOKENS
            m.compilation = is_various or (bool(m.album_artist) and bool(primary_artist) and norm_aa != norm_pa)

        m.set_if_empty("media_type", 1)

        # ── NUOVO: flag matched — determina se il file va rinominato col
        # titolo "arricchito" (match confermato da un provider) o col
        # titolo originale grezzo (nessun match affidabile trovato).
        m.matched = bool(
            m.itunes_track_id or m.mb_track_id or m.isrc or m._ytmusic_score >= 0.90
        )
        song.matched = m.matched

    # ── cover ────────────────────────────────────────────────────────────────

    def _fetch_cover(self, song: Song) -> Optional[bytes]:
        info = song.raw
        cover_future = self.cover_executor.submit(
            self.covers.fetch_cascade,
            mbid=song.meta.mb_album_id,
            rgid=song.meta.mb_release_group_id,
            ytdlp_url=info.get("thumbnail", "") or "",
            meta_cover_url=song.meta.cover_url,
        )
        try:
            return cover_future.result(timeout=self.cover_timeout)
        except Exception as exc:
            self.logger.warning(f"[SongProcessor] Cover fallita {song.video_id}: {exc}")
            return None

    # ── rename helper ────────────────────────────────────────────────────────

    def _build_rename_meta(self, song: Song) -> dict:
        """
        Se la song non è stata confermata da nessun provider (song.matched
        False), usa titolo/artista GREZZI (song.raw) per il rename invece
        del titolo "pulito"/arricchito in song.meta — così un match errato
        o assente non sovrascrive il nome file con dati inventati/sbagliati.
        """
        rename_meta = dict(song.meta.to_dict())
        if not song.matched:
            raw_title  = song.raw.get("title", "") or song.meta.title
            raw_artist = song.raw.get("artist", "") or song.meta.artist
            rename_meta["title"]  = raw_title
            rename_meta["artist"] = raw_artist
            self.logger.debug(
                f"[SongProcessor] Nessun match confermato, rename con titolo originale: {raw_title!r}"
            )
        return rename_meta

    # ── pipeline ─────────────────────────────────────────────────────────────

    async def _process_async(self, song: Song) -> None:
        seed = self._build_seed(song)
        song.meta.apply(seed)

        try:
            await self.pipeline.run(song)
        except Exception as exc:
            self.logger.error(f"[SongProcessor] Pipeline fallita {song.video_id}: {exc}", exc_info=True)

        self._postprocess_meta(song)

        if not song.path_current or not os.path.isfile(song.path_current):
            song.mark_error(f"File non trovato: {song.path_current!r}")
            self.logger.error(f"[SongProcessor] {song.error}")
            return

        cover = self._fetch_cover(song)

        try:
            song.write_tags(cover_override=cover)
        except Exception as exc:
            song.mark_error(f"write_tags fallito: {exc}")
            self.logger.error(f"[SongProcessor] {song.error}")
            return

        try:
            rename_meta = self._build_rename_meta(song)
            final_path = self.files.rename_file(song.path_current, rename_meta, self.output_dir)
        except Exception as exc:
            self.logger.warning(f"[SongProcessor] Rename fallito {song.video_id}: {exc}")
            final_path = song.path_current

        song.finalize_path(final_path)
        song.meta.dump_meta(self.logger)
        self.logger.info(f"[SongProcessor] Completato: {song.video_id}")

    # ── shutdown ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        async def _close_all():
            if hasattr(self.pipeline, "aclose"):
                await self.pipeline.aclose()
                return
            for attr in ("mb", "itunes", "deezer"):
                p = getattr(self.pipeline, attr, None)
                if p and hasattr(p, "close"):
                    try:
                        await p.close()
                    except Exception as exc:
                        self.logger.debug(f"[SongProcessor] close() {attr}: {exc}")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(_close_all())
            return

        new_loop = asyncio.new_event_loop()
        try:
            new_loop.run_until_complete(_close_all())
        except Exception as exc:
            self.logger.debug(f"[SongProcessor] close(): {exc}")
        finally:
            new_loop.close()