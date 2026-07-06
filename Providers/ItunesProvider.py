from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from Algorithm.RegexToken import EditionTokens, RemixTokens
from Algorithm.BestMatch import score_candidate, strip_parenthetical as _strip_parenthetical
from Helpers.MetaMapper import MetaMapper
from Model.Song import Song
from Algorithm.TextCleaner import TextCleaner
from Pipeline.ReleaseEdition import ReleaseEdition

from Providers.Providers_Helper.ItunesProviderHelper import ITunesProviderHelper
from Database.Model.ItunesModel import AppleMusicAlbum, AppleMusicTrack
from Database.Service.AlbumService import AlbumService
from Database.Service.TrackService import TrackService
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ItunesProviderUrls:
    BASE: str = "https://itunes.apple.com"
    SEARCH: str = BASE + "/search"
    LOOKUP: str = BASE + "/lookup"


@dataclass
class _SearchCandidate:
    """Coppia (score, item) con edizione calcolata una sola volta."""
    score: float
    item: Dict[str, Any]
    edition: ReleaseEdition


class ITunesProvider:

    SIM_TITLE_MIN  = 0.75
    SIM_ARTIST_MIN = 0.85
    SIM_ALBUM_MIN  = 0.85
    HIGH_CONFIDENCE = 0.90

    ITUNES_LIMIT: int = 500

    _FALLBACK_COUNTRIES: List[str] = ["US", "IT", "ES"]

    # ------------------------------------------------------------------
    # Costruttore
    # ------------------------------------------------------------------
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
        self._client                = session
        self._owns_client           = session is None
        self.logger                 = logger or logging.getLogger(__name__)
        self.country                = country.upper()
        self.prefer_album           = prefer_album
        self.prefer_explicit        = prefer_explicit
        self._min_request_interval  = max(5.0, min_request_interval)
        self._last_request_time     = 0.0
        self._request_lock          = asyncio.Lock()
        self.db_session: Optional[AsyncSession] = db_session
        self._collection_id_cache: Dict[str, int] = {}
        self._collection_id_cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    def set_country_for_artist(self, country: str) -> None:
        if country:
            self.country = country.upper()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    async def _throttle(self) -> None:
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self._min_request_interval:
                await asyncio.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.monotonic()

    async def _get_with_retry(self, url: str, params: Dict, max_retries: int = 3) -> List[Dict]:
        for attempt in range(max_retries):
            try:
                await self._throttle()
                response = await self.client.get(url, params=params)
                if response.status_code in (429, 403):
                    wait = 2 ** attempt + 1
                    self.logger.warning(
                        f"[iTunes] Rate limit ({response.status_code}), attendo {wait}s "
                        f"(tentativo {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                if response.status_code != 200:
                    self.logger.debug(f"[iTunes] HTTP {response.status_code} — {url} {params}")
                    return []
                try:
                    return response.json().get("results", [])
                except ValueError as exc:
                    self.logger.debug(f"[iTunes] JSON non valido: {exc}")
                    return []
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.logger.debug(f"[iTunes] Tentativo {attempt + 1} fallito: {exc}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        return []

    async def _get(self, url: str, params: Dict, use_fallback_countries: bool = True) -> List[Dict]:
        countries = [self.country]
        if use_fallback_countries:
            countries += [c for c in self._FALLBACK_COUNTRIES if c != self.country]

        for country in countries:
            results = await self._get_with_retry(url, {**params, "country": country})
            if results:
                if country != self.country:
                    self.logger.debug(f"[iTunes] Usato paese fallback: {country}")
                return results
        return []

    # ------------------------------------------------------------------
    # Query primitives
    # ------------------------------------------------------------------
    async def _search_songs(self, term: str, limit: int = ITUNES_LIMIT, attribute: str = "") -> List[Dict]:
        params = {
            "term": term, "media": "music", "entity": "song",
            "limit": limit, "version": 2, "explicit": "Yes",
        }
        if attribute:
            params["attribute"] = attribute
        return await self._get(ItunesProviderUrls.SEARCH, params)

    async def _search_albums(self, term: str, limit: int = 10) -> List[Dict]:
        return await self._get(ItunesProviderUrls.SEARCH, {
            "term": term, "media": "music", "entity": "album",
            "attribute": "albumTerm", "limit": limit, "version": 2,
        })

    async def lookup_artist_songs(self, artist_id: int, limit: int = ITUNES_LIMIT, sort: str = "recent") -> List[Dict]:
        params: Dict[str, Any] = {"id": artist_id, "entity": "song", "limit": limit}
        if sort:
            params["sort"] = sort
        return await self._get(ItunesProviderUrls.LOOKUP, params)

    async def lookup_album_songs(self, collection_id: int) -> List[Dict]:
        """DB-first: se le tracce sono già in cache locale, evita la chiamata a iTunes."""
        self.logger.debug(f"[iTunes] Lookup tracce per collectionId={collection_id}")

        if self.db_session:
            db_tracks = await TrackService.get_by_album(self.db_session, collection_id)
            if db_tracks:
                self.logger.debug(f"[iTunes] Cache DB hit collectionId={collection_id} ({len(db_tracks)} tracce)")
                return [self._track_model_to_dict(t) for t in db_tracks]

        return await self._get(ItunesProviderUrls.LOOKUP, {"id": collection_id, "entity": "song"})

    @staticmethod
    def _track_model_to_dict(t: AppleMusicTrack) -> Dict:
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

    async def lookup_artist_id(self, artist_name: str) -> Optional[int]:
        results = await self._get(ItunesProviderUrls.SEARCH, {
            "term": artist_name, "media": "music", "entity": "musicArtist",
            "attribute": "artistTerm", "limit": 5, "version": 2,
        })
        norm_target = TextCleaner.normalize(artist_name)
        for item in results:
            if item.get("wrapperType") == "artist" and TextCleaner.normalize(item.get("artistName", "")) == norm_target:
                return item.get("artistId")
        return next((item.get("artistId") for item in results if item.get("artistId")), None)

    # ------------------------------------------------------------------
    # Candidate filtering & scoring (singola pipeline, usata da tutte le strategie)
    # ------------------------------------------------------------------
    def _evaluate_candidates(
        self,
        results: List[Dict],
        *,
        title: str,
        title_norm: str,
        art_primary: str,
        hint_album_norm: str = "",
        duration_ms: Optional[int] = None,
        min_score: float = 0.5,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        norm_art = TextCleaner.normalize(art_primary) if art_primary else ""
        candidates: List[_SearchCandidate] = []

        for item in results:
            if item.get("wrapperType") != "track" or item.get("kind") not in ("song", "music-video"):
                continue
            if not item.get("trackName"):
                continue
            if not self._passes_prefilters(item, title, title_norm, hint_album_norm):
                continue

            score = score_candidate(
                title=title_norm, artist=norm_art, album_hint=hint_album_norm,
                duration_ms=duration_ms, isrc="", candidate=item, min_score=min_score,
            )
            if score is None:
                continue
            if not self._passes_final_filters(item, score, title, title_norm, art_primary, hint_album_norm):
                continue

            edition = ReleaseEdition.from_collection(
                collection_type=item.get("collectionType", ""),
                collection_name=item.get("collectionName", ""),
                track_count=item.get("trackCount") or 0,
                title_norm=title_norm,
            )
            candidates.append(_SearchCandidate(score=score, item=item, edition=edition))

        best = self._pick_best(candidates, duration_ms=duration_ms)
        if best is None:
            return None, -1.0, None

        best = self._maybe_swap_album_single(best, candidates, title_norm, norm_art)
        return best.item, best.score, best.edition

    def _passes_prefilters(self, item: Dict, title: str, title_norm: str, hint_album_norm: str) -> bool:
        """Filtri di esclusione rapidi, prima dello scoring costoso."""
        if ITunesProviderHelper.is_artist_blacklisted(
            TextCleaner.normalize(item.get("artistName", "")), title_norm
        ):
            return False

        album_sim_pre = (
            TextCleaner.album_edition_similarity(
                hint_album_norm, TextCleaner.normalize(item.get("collectionName", ""))
            ) if hint_album_norm else 0.0
        )
        if ITunesProviderHelper.is_compilation_item(item, album_sim_pre):
            return False

        if re.search(r'[\(\[]\s*mixed\s*[\)\]]', item.get("trackName", ""), re.IGNORECASE):
            return False

        # NOTA (fix): qui c'era un hard-reject su
        # `RemixTokens.has(title) != RemixTokens.has(item.get("trackName", ""))`.
        # Causava un reject totale di QUALSIASI candidato quando il titolo
        # cercato aveva un tag remix tra parentesi (es. "Leaked (Remix)") ma
        # il provider catalogava la recording col titolo "pulito" (es. solo
        # "Leaked") — caso comune su iTunes/MB. Il mismatch remix è già
        # penalizzato in modo proporzionale da `remix_mismatch` dentro
        # `score_candidate` (Algorithm/BestMatch.py): un hard-reject qui era
        # ridondante e troppo aggressivo. Rimosso: lasciamo che lo scoring
        # decida, coerentemente con come viene già trattato `live_mismatch`.

        # Confronto sul titolo "core" (senza tag tra parentesi/brackets come
        # remix/live/acoustic): un tag di versione non deve impedire il
        # match qui — ci pensa già `remix_mismatch`/`live_mismatch` dentro
        # score_candidate, in modo proporzionale invece che con un reject
        # secco. Senza questo, "Leaked (Remix)" veniva scartato a priori
        # contro un candidato catalogato come solo "Leaked".
        title_clean_norm = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(title)))
        cand_clean_norm  = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(item.get("trackName", ""))))
        if TextCleaner.title_similarity(title_clean_norm, cand_clean_norm) < 0.88:
            return False

        return True

    def _passes_final_filters(
        self, item: Dict, score: float, title: str, title_norm: str,
        art_primary: str, hint_album_norm: str,
    ) -> bool:
        # Confronto sul titolo "core" (vedi nota in _passes_prefilters): un
        # tag tra parentesi nel titolo cercato (remix/live/ecc.) non deve
        # abbassare artificialmente questa similarity sotto SIM_TITLE_MIN
        # quando il candidato ha semplicemente il titolo senza quel tag.
        core_title_norm = TextCleaner.normalize(_strip_parenthetical(title_norm))
        cand_title_norm = TextCleaner.normalize(_strip_parenthetical(TextCleaner.clean_title(item.get("trackName", ""))))
        title_sim = TextCleaner.title_similarity(core_title_norm, cand_title_norm)
        if title_sim < self.SIM_TITLE_MIN and cand_title_norm != core_title_norm:
            return False

        if art_primary:
            norm_art      = TextCleaner.normalize(art_primary)
            norm_cand_art = TextCleaner.normalize(item.get("artistName", ""))
            norm_coll_art = TextCleaner.normalize(item.get("collectionArtistName", ""))
            artist_sim    = TextCleaner.title_similarity(norm_art, norm_cand_art)
            coll_sim      = TextCleaner.title_similarity(norm_art, norm_coll_art) if norm_coll_art else 0.0
            substring_match = (
                re.search(rf'\b{re.escape(norm_art)}\b', norm_cand_art)
                or (norm_coll_art and (norm_art in norm_coll_art or norm_coll_art in norm_art))
            )
            if artist_sim < self.SIM_ARTIST_MIN and coll_sim < self.SIM_ARTIST_MIN and not substring_match:
                return False

        if score >= self.HIGH_CONFIDENCE:
            return True

        if hint_album_norm:
            cand_album_norm = TextCleaner.normalize(item.get("collectionName", ""))
            album_sim = TextCleaner.album_edition_similarity(hint_album_norm, cand_album_norm)
            hint_stripped = re.sub(r'[^a-z0-9\s]', '', hint_album_norm).strip()
            cand_stripped = re.sub(r'[^a-z0-9\s]', '', cand_album_norm).strip()
            hint_is_prefix = bool(cand_stripped) and hint_stripped.startswith(cand_stripped)
            artist_exact = (
                art_primary
                and TextCleaner.title_similarity(
                    TextCleaner.normalize(art_primary), TextCleaner.normalize(item.get("artistName", ""))
                ) >= self.SIM_ARTIST_MIN
            )
            if album_sim < self.SIM_ALBUM_MIN and score < 0.95 and not hint_is_prefix and not artist_exact:
                return False

        return True

    def _pick_best(
        self, candidates: List[_SearchCandidate], duration_ms: Optional[int] = None,
    ) -> Optional[_SearchCandidate]:
        if not candidates:
            return None

        def _is_preferred_explicitness(c: _SearchCandidate) -> bool:
            is_explicit = str(c.item.get("trackExplicitness", "")).lower() == "explicit"
            return is_explicit if self.prefer_explicit else not is_explicit

        def _duration_delta(c: _SearchCandidate) -> int:
            if duration_ms is None:
                return 0
            cand_ms = c.item.get("trackTimeMillis")
            return abs(int(duration_ms) - int(cand_ms)) if cand_ms else 999_999

        preferred: Dict[int, _SearchCandidate] = {}
        fallback: Dict[int, _SearchCandidate] = {}
        orphans: List[_SearchCandidate] = []

        for c in candidates:
            cid = c.item.get("collectionId")
            bucket = preferred if _is_preferred_explicitness(c) else fallback
            if cid:
                if cid not in bucket or c.score > bucket[cid].score:
                    bucket[cid] = c
            else:
                orphans.append(c)

        pool = list(preferred.values()) + [c for cid, c in fallback.items() if cid not in preferred] + orphans

        def _sort_key(c: _SearchCandidate) -> Tuple[float, bool, int]:
            return (c.score, not ITunesProviderHelper.is_live_item(c.item), -_duration_delta(c))

        return max(pool, key=_sort_key)

    def _maybe_swap_album_single(
        self,
        best: _SearchCandidate,
        candidates: List[_SearchCandidate],
        title_norm: str,
        norm_art: str,
    ) -> _SearchCandidate:
        """Se la preferenza prefer_album è impostata e best non la rispetta, cerca un'alternativa equivalente."""
        want_album  = self.prefer_album and best.edition.is_short_form
        want_single = not self.prefer_album and best.edition.kind.value == "album"
        if not want_album and not want_single:
            return best

        best_title_norm  = TextCleaner.normalize(TextCleaner.clean_title(best.item.get("trackName", "")))
        best_artist_norm = TextCleaner.normalize(best.item.get("artistName", ""))
        target_is_short  = want_single

        alt, alt_score = None, -1.0
        for c in candidates:
            is_short = c.edition.is_short_form
            if is_short != target_is_short:
                continue
            it_norm = TextCleaner.normalize(TextCleaner.clean_title(c.item.get("trackName", "")))
            ia_norm = TextCleaner.normalize(c.item.get("artistName", ""))
            if (
                TextCleaner.title_similarity(best_title_norm, it_norm) > 0.9
                and TextCleaner.title_similarity(best_artist_norm, ia_norm) > 0.8
                and c.score > alt_score
            ):
                alt, alt_score = c, c.score

        return alt if alt else best

    # ------------------------------------------------------------------
    # Album resolution
    # ------------------------------------------------------------------
    def _best_album_id(self, album_results: List[Dict], hint_album_norm: str, art_primary: str) -> Optional[int]:
        best_sim, best_id, best_name_norm, best_edition_overlap = -1.0, None, "", 0
        hint_editions = EditionTokens.findall(hint_album_norm)

        for alb in album_results:
            alb_artist = alb.get("collectionArtistName") or alb.get("artistName", "")
            if art_primary and alb_artist:
                norm_primary = TextCleaner.normalize(art_primary)
                norm_alb_art = TextCleaner.normalize(alb_artist)
                artist_sim = TextCleaner.title_similarity(norm_primary, norm_alb_art)
                if not (artist_sim >= 0.7 or norm_primary in norm_alb_art or norm_alb_art in norm_primary):
                    continue

            alb_name_norm   = TextCleaner.normalize(alb.get("collectionName", ""))
            cand_editions   = EditionTokens.findall(alb_name_norm)
            edition_overlap = len(hint_editions & cand_editions)

            edition_sim = TextCleaner.album_edition_similarity(hint_album_norm, alb_name_norm)
            edition_mismatch_penalty = 0.2 if (hint_editions != cand_editions) else 0.0
            sim = max(0.0, edition_sim - edition_mismatch_penalty)

            min_sim = 0.70 if hint_editions else self.SIM_ALBUM_MIN
            if sim <= min_sim:
                continue

            is_better = (
                sim > best_sim + 0.05
                or (abs(sim - best_sim) <= 0.05 and edition_overlap > best_edition_overlap)
                or (
                    abs(sim - best_sim) <= 0.05
                    and edition_overlap == best_edition_overlap
                    and len(alb_name_norm) > len(best_name_norm)
                )
            )
            if is_better:
                best_sim, best_id, best_name_norm, best_edition_overlap = (
                    sim, alb.get("collectionId"), alb_name_norm, edition_overlap
                )

        return best_id

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
            results = await self._search_albums(term, limit=10)
            cid = self._best_album_id(results, hint_album_norm, art_primary)
            if cid:
                return cid

        # ultimo tentativo senza vincolo artista
        results = await self._search_albums(hint_clean, limit=10)
        return self._best_album_id(results, hint_album_norm, "")

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

    # ------------------------------------------------------------------
    # Le 3 strategie (sostituiscono le 7 precedenti)
    # ------------------------------------------------------------------
    async def _strategy_album_hint(
        self, *, hint_album: str, hint_album_norm: str, art_primary: str,
        title: str, title_norm: str, duration_ms: Optional[int], min_score: float,
    ) -> Tuple[Optional[Dict], float, Optional[ReleaseEdition]]:
        """
        Usa l'album conosciuto per restringere la ricerca: prima un collectionId
        già visto in cache, poi una resolve fresca via ricerca album.
        Copre i casi prima divisi tra _strategy_album_first/_result_cache/_collection_lookup.
        """
        if not hint_album:
            return None, -1.0, None

        collection_id = self._collection_id_cache.get(hint_album_norm)
        if not collection_id:
            collection_id = await self._resolve_album_id(hint_album, hint_album_norm, art_primary)

        if not collection_id:
            return None, -1.0, None

        tracks = await self.lookup_album_songs(collection_id)
        if not tracks:
            return None, -1.0, None

        item, score, edition = self._evaluate_candidates(
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
        """
        Ricerca diretta per titolo (+ artista se disponibile), con fallback
        progressivi: artista primario → tutti gli artisti (feat incluso) →
        solo titolo. Copre i casi prima divisi tra _global_search/_targeted_album/_emergency_title_only.
        """
        term = f"{art_primary} {cleaned_title}".strip() if art_primary else cleaned_title
        results = await self._search_songs(term)

        if not results and art_primary:
            results = await self._search_songs(f"{art_primary} {cleaned_title}", limit=10, attribute="artistTerm")

        valid = results and ITunesProviderHelper.has_valid_candidate(
            results, title_norm, art_primary, self.SIM_TITLE_MIN, self.SIM_ARTIST_MIN
        )
        if not valid and artist and artist != art_primary:
            all_artists = re.sub(r'\s*[,&]\s*', ' ', artist).strip()
            results_fb = await self._search_songs(f"{all_artists} {cleaned_title}".strip(), limit=50)
            if results_fb:
                results = results_fb

        if not results:
            results = await self._search_songs(cleaned_title, limit=10, attribute="songTerm")

        if not results:
            return None, -1.0, None

        item, score, edition = self._evaluate_candidates(
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
        """
        Ultima risorsa: scarica l'intero catalogo recente (poi completo) di un
        artista e valuta tutti i suoi brani. Copre il vecchio _strategy_artist_lookup.
        """
        if not art_primary:
            return None, -1.0, None

        artist_id = known_artist_id or await self.lookup_artist_id(art_primary)
        if not artist_id:
            return None, -1.0, None

        best_item, best_score, best_edition = None, -1.0, None
        for sort in ("recent", ""):
            songs = await self.lookup_artist_songs(artist_id, sort=sort)
            item, score, edition = self._evaluate_candidates(
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
    # Persistenza DB (solo best pick)
    # ------------------------------------------------------------------
    @staticmethod
    def _album_record_from_track(track: Dict) -> Optional[Dict]:
        cid = track.get("collectionId")
        if not cid:
            return None
        return {
            "wrapperType": "collection", "collectionId": cid,
            "collectionType": track.get("collectionType"), "artistId": track.get("artistId"),
            "artistName": track.get("collectionArtistName") or track.get("artistName"),
            "collectionName": track.get("collectionName"), "collectionViewUrl": track.get("collectionViewUrl"),
            "collectionExplicitness": track.get("collectionExplicitness"), "trackCount": track.get("trackCount"),
            "country": track.get("country"), "primaryGenreName": track.get("primaryGenreName"),
        }

    async def _persist_lookup_results(self, results: list, known_isrc: str = "") -> None:
        if not self.db_session or not results:
            return
        try:
            album_dicts = [r for r in results if r.get("wrapperType") == "collection" and r.get("collectionId")]
            track_dicts = [r for r in results if r.get("wrapperType") == "track" and r.get("trackId")]

            for a in album_dicts:
                album = AppleMusicAlbum(
                    collection_id=a["collectionId"], wrapper_type=a.get("wrapperType"),
                    collection_type=a.get("collectionType") or "Album", artist_id=a.get("artistId"),
                    artist_name=a.get("artistName"), collection_name=a.get("collectionName"),
                    collection_view_url=a.get("collectionViewUrl"),
                    collection_explicitness=a.get("collectionExplicitness"),
                    track_count=a.get("trackCount"), country=a.get("country", self.country),
                    primary_genre_name=a.get("primaryGenreName"),
                )
                await AlbumService.save(self.db_session, album)

            if track_dicts:
                tracks = []
                for t in track_dicts:
                    track = AppleMusicTrack(
                        track_id=t["trackId"], artist_id=t.get("artistId"), collection_id=t.get("collectionId"),
                        artist_name=t.get("artistName"), collection_name=t.get("collectionName"),
                        track_name=t.get("trackName"), collection_artist_name=t.get("collectionArtistName"),
                        track_explicitness=t.get("trackExplicitness"), disc_count=t.get("discCount"),
                        disc_number=t.get("discNumber"), track_count=t.get("trackCount"),
                        track_number=t.get("trackNumber"), track_time_millis=t.get("trackTimeMillis"),
                        primary_genre_name=t.get("primaryGenreName"),
                        isrc=known_isrc.upper() if known_isrc else None,
                    )
                    track.artwork_url = t.get("artworkUrl100")
                    tracks.append(track)
                await TrackService.bulk_upsert(self.db_session, tracks)
        except Exception as exc:
            self.logger.warning(f"[iTunes] Errore persistenza lookup results: {exc}", exc_info=True)

    async def _persist_best_result(self, item: Optional[Dict], known_isrc: str = "") -> None:
        if not self.db_session or not item:
            return
        if item.get("wrapperType") != "track" or not item.get("trackId"):
            return
        album_record = self._album_record_from_track(item)
        if album_record:
            await self._persist_lookup_results([album_record], known_isrc=known_isrc)
        await self._persist_lookup_results([item], known_isrc=known_isrc)

    async def persist_track_isrc(self, track_id: int, isrc: str) -> None:
        """Aggiorna l'ISRC su una traccia già persistita. Non sovrascrive se già presente."""
        if not self.db_session or not track_id or not isrc:
            return
        try:
            from sqlalchemy import update
            stmt = (
                update(AppleMusicTrack)
                .where(AppleMusicTrack.track_id == track_id)
                .where(AppleMusicTrack.isrc.is_(None))
                .values(isrc=isrc.upper())
            )
            await self.db_session.execute(stmt)
            await self.db_session.commit()
            self.logger.debug(f"[iTunes] ISRC {isrc!r} → track_id={track_id}")
        except Exception as exc:
            self.logger.debug(f"[iTunes] persist_track_isrc fallito: {exc}")

    # ------------------------------------------------------------------
    # Output formatting
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
            await self.lookup_album_songs(cid)
        await self._persist_best_result(best)
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
            self.country = target_country

        try:
            return await self._execute_search(title, song, artist, hint_album, duration_ms, min_score=min_score)
        finally:
            if self.country != original_country:
                self.country = original_country

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