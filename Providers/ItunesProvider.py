from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from Model.Song import Song
from Algorithm.TextCleaner import TextCleaner
from Pipeline.ReleaseEdition import ReleaseEdition

from Providers.Itunes.ItunesHelper import ITunesProviderHelper
from Providers.Itunes.ItunesHttpClient import ITunesHttpClient, ItunesProviderUrls
from Providers.Itunes.ItunesCandidateEvaluator import ITunesCandidateEvaluator
from Providers.Itunes.ItunesPersistance import ITunesPersistence
from Helpers.MetaMapper import MetaMapper
from sqlalchemy.ext.asyncio import AsyncSession


class ITunesProvider:
    """
    Orchestratore delle 3 strategie di ricerca iTunes. Delega:
    - HTTP/throttle/retry           -> ITunesHttpClient
    - scoring/filtering candidati    -> ITunesCandidateEvaluator
    - persistenza DB                 -> ITunesPersistence

    API pubblica invariata rispetto alla versione monolitica precedente.
    """

    HIGH_CONFIDENCE = 0.90
    ITUNES_LIMIT: int = 500

    def __init__(
        self,
        session: Optional[httpx.AsyncClient] = None,
        logger: Optional[logging.Logger] = None,
        prefer_album: bool = False,
        min_request_interval: float = 1.0,
        prefer_explicit: bool = True,
        country: str = "US",
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.country = country.upper()

        self.http = ITunesHttpClient(
            session=session, logger=self.logger, min_request_interval=min_request_interval,
            country=self.country, db_session=db_session,
        )
        self.evaluator = ITunesCandidateEvaluator(prefer_album=prefer_album, prefer_explicit=prefer_explicit)
        self.persistence = ITunesPersistence(db_session=db_session, logger=self.logger, country=self.country)

        self.db_session: Optional[AsyncSession] = db_session
        self._collection_id_cache: Dict[str, int] = {}
        import asyncio
        self._collection_id_cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        await self.http.close()

    def set_country_for_artist(self, country: str) -> None:
        if country:
            self.country = country.upper()
            self.http.set_country(country)
            self.persistence.country = self.country

    # ------------------------------------------------------------------
    # Passthrough lookups (usati dal pipeline / MetadataPipeline)
    # ------------------------------------------------------------------
    async def lookup_album_songs(self, collection_id: int) -> List[Dict]:
        return await self.http.lookup_album_songs(collection_id)

    async def lookup_artist_songs(self, artist_id: int, limit: int = ITUNES_LIMIT, sort: str = "recent") -> List[Dict]:
        return await self.http.lookup_artist_songs(artist_id, limit=limit, sort=sort)

    async def lookup_artist_id(self, artist_name: str) -> Optional[int]:
        return await self.http.lookup_artist_id(artist_name)

    async def persist_track_isrc(self, track_id: int, isrc: str) -> None:
        await self.persistence.persist_track_isrc(track_id, isrc)

    # ------------------------------------------------------------------
    # Collection id cache (per hint_album_norm)
    # ------------------------------------------------------------------
    async def _cache_collection_id(self, hint_album_norm: str, item: Dict) -> None:
        if not hint_album_norm:
            return
        cid = item.get("collectionId")
        if not cid:
            return
        async with self._collection_id_cache_lock:
            self._collection_id_cache.setdefault(hint_album_norm, cid)

    async def _resolve_album_id(self, hint_album: str, hint_album_norm: str, art_primary: str) -> Optional[int]:
        """Trova il collectionId più plausibile per hint_album, con fallback progressivi."""
        hint_clean = re.sub(r'[\(\)\[\]]', ' ', hint_album)
        hint_clean = re.sub(r'\s*-\s*', ' ', hint_clean)
        hint_clean = re.sub(r'\s{2,}', ' ', hint_clean).strip()
        term_clean = f"{art_primary} {hint_clean}".strip() if art_primary else hint_clean

        attempts = [term_clean, hint_clean]
        if art_primary:
            attempts.append(art_primary)

        for term in attempts:
            results = await self.http.search_albums(term, limit=10)
            cid = self.evaluator.best_album_id(results, hint_album_norm, art_primary)
            if cid:
                return cid

        results = await self.http.search_albums(hint_clean, limit=10)
        return self.evaluator.best_album_id(results, hint_album_norm, "")

    # ------------------------------------------------------------------
    # Le 3 strategie
    # ------------------------------------------------------------------
    async def _strategy_album_hint(
        self, *, hint_album: str, hint_album_norm: str, art_primary: str,
        title: str, title_norm: str, duration_ms: Optional[int], min_score: float,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        if not hint_album:
            return None, -1.0, None

        collection_id = self._collection_id_cache.get(hint_album_norm)
        if not collection_id:
            collection_id = await self._resolve_album_id(hint_album, hint_album_norm, art_primary)

        if not collection_id:
            return None, -1.0, None

        tracks = await self.http.lookup_album_songs(collection_id)
        if not tracks:
            return None, -1.0, None

        item, score, edition = self.evaluator.evaluate(
            tracks, title=title, title_norm=title_norm, art_primary=art_primary,
            hint_album_norm=hint_album_norm, duration_ms=duration_ms, min_score=min_score,
        )
        if item:
            await self._cache_collection_id(hint_album_norm, item)
        return item, score, edition

    async def _strategy_global_search(
        self, *, art_primary: str, artist: str, cleaned_title: str, title: str,
        title_norm: str, hint_album_norm: str, duration_ms: Optional[int], min_score: float,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        term = f"{art_primary} {cleaned_title}".strip() if art_primary else cleaned_title
        results = await self.http.search_songs(term)

        if not results and art_primary:
            results = await self.http.search_songs(f"{art_primary} {cleaned_title}", limit=10, attribute="artistTerm")

        valid = results and ITunesProviderHelper.has_valid_candidate(
            results, title_norm, art_primary, self.evaluator.SIM_TITLE_MIN, self.evaluator.SIM_ARTIST_MIN
        )
        if not valid and artist and artist != art_primary:
            all_artists = re.sub(r'\s*[,&]\s*', ' ', artist).strip()
            results_fb = await self.http.search_songs(f"{all_artists} {cleaned_title}".strip(), limit=50)
            if results_fb:
                results = results_fb

        if not results:
            results = await self.http.search_songs(cleaned_title, limit=10, attribute="songTerm")

        if not results:
            return None, -1.0, None

        item, score, edition = self.evaluator.evaluate(
            results, title=title, title_norm=title_norm, art_primary=art_primary,
            hint_album_norm=hint_album_norm, duration_ms=duration_ms, min_score=min_score,
        )
        if item:
            await self._cache_collection_id(hint_album_norm, item)
        return item, score, edition

    async def _strategy_artist_catalog(
        self, *, art_primary: str, title: str, title_norm: str, hint_album_norm: str,
        duration_ms: Optional[int], min_score: float, known_artist_id: Optional[int] = None,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        if not art_primary:
            return None, -1.0, None

        artist_id = known_artist_id or await self.http.lookup_artist_id(art_primary)
        if not artist_id:
            return None, -1.0, None

        best_item, best_score, best_edition = None, -1.0, None
        for sort in ("recent", ""):
            songs = await self.http.lookup_artist_songs(artist_id, sort=sort)
            item, score, edition = self.evaluator.evaluate(
                songs, title=title, title_norm=title_norm, art_primary=art_primary,
                hint_album_norm=hint_album_norm, duration_ms=duration_ms, min_score=min_score,
            )
            if item and score > best_score:
                best_item, best_score, best_edition = item, score, edition
                await self._cache_collection_id(hint_album_norm, item)
            if best_score >= self.HIGH_CONFIDENCE:
                break

        return best_item, best_score, best_edition

    # ------------------------------------------------------------------
    # Output formatting + finalizzazione
    # ------------------------------------------------------------------
    def _format_result(
        self, item: Dict, default_title: str, default_artist: str, edition: Optional[ReleaseEdition],
    ) -> Dict[str, Any]:
        mapped = MetaMapper.from_itunes(
            item=item, default_title=default_title, default_artist=default_artist, logger=self.logger,
        )
        mapped["_release_edition"] = edition
        return mapped

    async def _finalize_and_persist(
        self, best: Dict, title: str, artist: str, edition: Optional[ReleaseEdition],
    ) -> Dict[str, Any]:
        cid = best.get("collectionId")
        if cid:
            await self.http.lookup_album_songs(cid)
        await self.persistence.persist_best_result(best)
        await self._cache_collection_id(TextCleaner.normalize(best.get("collectionName", "")), best)
        return self._format_result(best, title, artist, edition)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def search(
        self,
        title: str,
        song: Song,
        artist: str = "",
        hint_album: str = "",
        playlist_id: str = "",
        markets: Optional[List[str]] = None,
        duration_ms: Optional[int] = None,
        trusted_hint_album: bool = False,
        min_score: float = 0.5,
    ) -> Dict[str, Any]:
        if not title or not title.strip():
            return {}

        original_country = self.country
        target_country = next((c for c in ("IT", "US", "GB", "ES", "FR") if markets and c in markets), None)
        if target_country and target_country != self.country:
            self.set_country_for_artist(target_country)

        try:
            return await self._execute_search(title, song, artist, hint_album, duration_ms, min_score=min_score)
        finally:
            if self.country != original_country:
                self.set_country_for_artist(original_country)

    async def _execute_search(
        self, title: str, song: Song, artist: str = "", hint_album: str = "",
        duration_ms: Optional[int] = None, min_score: float = 0.5,
    ) -> Dict[str, Any]:
        if not title or not title.strip():
            return {}

        if artist and TextCleaner.looks_like_label(artist):
            new_artist, new_title = TextCleaner.extract_artist_from_title(title, artist)
            if new_artist != artist:
                artist, title = new_artist, new_title

        title, artist = TextCleaner.enrich_artist_from_title(title, artist)
        cleaned_title = TextCleaner.clean_title(title, artist)
        art_primary   = TextCleaner.primary_artist(artist) if artist else ""
        title_norm    = TextCleaner.normalize(cleaned_title)

        hint_album      = ITunesProviderHelper.sanitize_hint_album(hint_album, title, self.logger)
        hint_album_norm = TextCleaner.normalize(hint_album) if hint_album else ""

        best, best_score, best_edition = None, -1.0, None

        # Strategia 1: album hint (se disponibile)
        if hint_album:
            best, best_score, best_edition = await self._strategy_album_hint(
                hint_album=hint_album, hint_album_norm=hint_album_norm, art_primary=art_primary,
                title=title, title_norm=title_norm, duration_ms=duration_ms, min_score=min_score,
            )
            if best and best_score >= self.HIGH_CONFIDENCE:
                return await self._finalize_and_persist(best, title, artist, best_edition)

        # Strategia 2: ricerca globale
        item, score, edition = await self._strategy_global_search(
            art_primary=art_primary, artist=artist, cleaned_title=cleaned_title, title=title,
            title_norm=title_norm, hint_album_norm=hint_album_norm, duration_ms=duration_ms, min_score=min_score,
        )
        if item and score > best_score:
            best, best_score, best_edition = item, score, edition
        if best and best_score >= self.HIGH_CONFIDENCE:
            return await self._finalize_and_persist(best, title, artist, best_edition)

        # Strategia 3: catalogo artista (ultima risorsa)
        if art_primary and best_score < self.HIGH_CONFIDENCE:
            known_artist_id = best.get("artistId") if best else None
            item, score, edition = await self._strategy_artist_catalog(
                art_primary=art_primary, title=title, title_norm=title_norm, hint_album_norm=hint_album_norm,
                duration_ms=duration_ms, min_score=min_score, known_artist_id=known_artist_id,
            )
            if item and score > best_score:
                best, best_score, best_edition = item, score, edition

        if not best:
            return {}

        return await self._finalize_and_persist(best, title, artist, best_edition)