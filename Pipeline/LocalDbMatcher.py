from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from Algorithm.BestMatch import TrackMatcher, strip_parenthetical
from Algorithm.RegexToken import FakeAlbumSuffix
from Algorithm.TextCleaner import TextCleaner
from Database.Model.ItunesModel import AppleMusicAlbum, AppleMusicArtist, AppleMusicTrack
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
        min_score: float = MusicPatterns.DB_LOCAL_MIN_SCORE,
    ) -> None:
        self.session = session
        self.matcher = matcher or TrackMatcher(min_score=MusicPatterns.MATCHER_MIN_SCORE)
        self.log = logger or logging.getLogger(__name__)
        # Soglia di accettazione del DB-hit come step 1 della pipeline.
        # Distinta da self.matcher._min_score: quest'ultima è la soglia
        # "grezza" del TrackMatcher (usata anche altrove, es. iTunes/MB),
        # questa è la soglia più severa richiesta per bloccare l'intera
        # ricerca remota su un solo hit locale.
        self.min_score = min_score
        self._album_cache: Dict[int, AppleMusicAlbum] = {}
        # collection_id -> conteggio REALE di righe AppleMusicTrack per
        # quell'album (non il campo denormalizzato AppleMusicAlbum.track_count,
        # che può essere stale/assente per album con origine YouTube).
        # Popolata da _preload_track_counts, una sola query aggregata per
        # batch di candidati invece di N query separate.
        self._real_track_count_cache: Dict[int, int] = {}
        # artist_id -> record AppleMusicArtists (o None se non presente in
        # tabella). Popolata in batch da _preload_artists (single IN(...));
        # to_meta() usa _get_artist() come fallback puntuale se il record
        # non era nella batch (es. path ISRC a riga singola).
        self._artist_cache: Dict[int, Optional[AppleMusicArtist]] = {}

    # ── Public API ───────────────────────────────────────────────────

    async def find(
        self, title: str, artist: str = "", album_hint: str = "",
        duration_ms: Optional[int] = None, isrc: str = "",
    ) -> Optional[Tuple[AppleMusicTrack, float]]:
        """
        Priorità a titolo+artista+album (segnale diretto); ISRC solo come
        fallback quando title/artist non trovano nulla.

        Ritorna (track, score) solo se score >= self.min_score. Un match
        trovato ma sotto soglia è considerato un miss: nessuna riga viene
        ritornata, così il chiamante prosegue con iTunes/fallback invece
        di fidarsi di un hit locale debole.
        """
        if not title or not title.strip():
            return None

        q = self._build_query(title, artist, album_hint, duration_ms, isrc)
        try:
            hit = await self._find_by_title_artist(q)
            if hit:
                return hit
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

        artist = await self._get_artist(track.artist_id)
        if artist and artist.artist_name:
            # Nome artista canonico da AppleMusicArtists, MAI ripulito:
            # priorità massima in scrittura tag (vedi SongMetaWriter).
            # NON sostituisce "artist" (usato per album_artist/compilation/
            # sort fields): è un campo separato, dedicato solo al tag finale.
            mapped["raw_artist_name"] = artist.artist_name

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

    # ── Title/Artist/Album path ──────────────────────────────────────

    async def _find_by_title_artist(self, q: _Query) -> Optional[Tuple[AppleMusicTrack, float]]:
        rows = await self._candidate_rows(q.title_norm, q.artist_norm, q.hint_norm)
        if not rows:
            return None
        await self._preload_albums(rows)
        await self._preload_track_counts(rows)
        await self._preload_artists(rows)

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

        if best_track is None or best_score < self.min_score:
            if best_track is not None:
                self.log.debug(
                    f"[LocalDbMatcher] hit title/artist sotto soglia "
                    f"({best_score:.2f} < {self.min_score:.2f}), trattato come miss: "
                    f"'{best_track.track_name}'"
                )
            return None
        return best_track, best_score

    # ── ISRC path (fallback) ─────────────────────────────────────────

    async def _find_by_isrc(self, q: _Query) -> Optional[Tuple[AppleMusicTrack, float]]:
        stmt = select(AppleMusicTrack).where(AppleMusicTrack.isrc == q.isrc)
        rows = list((await self.session.execute(stmt)).scalars().all())
        if not rows:
            return None

        if q.has_remix:
            tagged = [r for r in rows if self._has_version_tag(r.track_name or "")]
            rows = tagged or rows

        # Score di similarità titolo, usato sia per restringere i candidati
        # con più righe sullo stesso ISRC, sia come punteggio da confrontare
        # con la soglia min_score (l'ISRC exact match NON bypassa più la
        # soglia: un ISRC può comparire su righe con titolo molto diverso,
        # es. remix vs originale, quindi va comunque validato testualmente).
        if q.title_norm:
            scored = [
                (TextCleaner.title_similarity(q.title_norm, TextCleaner.normalize(r.track_name or "")), r)
                for r in rows
            ]
            best_sim = max(s for s, _ in scored)
            rows = [r for s, r in scored if s >= best_sim - 0.05]
            title_score = best_sim
        else:
            # Nessun titolo da confrontare: non possiamo validare testualmente
            # l'ISRC, quindi non c'è un punteggio affidabile da opporre alla
            # soglia. Trattato come non abbastanza sicuro per lo step DB-first.
            title_score = 0.0

        if title_score < self.min_score:
            self.log.debug(
                f"[LocalDbMatcher] ISRC hit sotto soglia titolo "
                f"({title_score:.2f} < {self.min_score:.2f}), trattato come miss."
            )
            return None

        if len(rows) == 1:
            return rows[0], title_score

        await self._preload_albums(rows)
        await self._preload_track_counts(rows)
        await self._preload_artists(rows)
        best_row = max(rows, key=lambda r: self._edition_score(r, q))
        return best_row, title_score

    async def _candidate_rows(self, title_norm: str, artist_norm: str, hint_norm: str = "") -> list[AppleMusicTrack]:
        """
        Costruisce le condizioni ILIKE per titolo/artista/album e le
        combina con AND. Se l'AND risulta troppo restrittivo (0 righe),
        allarga progressivamente a OR — stesso pattern di fallback già
        usato quando c'erano solo titolo+artista, ora esteso al terzo
        campo (album) quando disponibile.
        """
        title_kw = self._keywords(title_norm)
        artist_kw = self._keywords(artist_norm)
        album_kw = self._keywords(hint_norm) if hint_norm else []
        if not title_kw and not artist_kw and not album_kw:
            return []

        conds = []
        if title_kw:
            conds.append(or_(*(AppleMusicTrack.track_name.ilike(f"%{w}%") for w in title_kw)))
        if artist_kw:
            conds.append(or_(*(AppleMusicTrack.artist_name.ilike(f"%{w}%") for w in artist_kw)))
        if album_kw:
            conds.append(or_(*(AppleMusicTrack.collection_name.ilike(f"%{w}%") for w in album_kw)))

        where = and_(*conds) if len(conds) > 1 else conds[0]
        stmt = select(AppleMusicTrack).where(where).limit(self.MAX_CANDIDATES)
        rows = list((await self.session.execute(stmt)).scalars().all())

        if not rows and len(conds) > 1:
            # AND(title, artist, album) troppo restrittivo: prova a
            # rilassare togliendo l'album prima di cadere a OR totale,
            # così un album-hint leggermente diverso (es. edizione
            # deluxe non presente nel DB) non fa perdere un match valido
            # su titolo+artista.
            if album_kw and len(conds) == 3:
                relaxed = and_(conds[0], conds[1])
                stmt = select(AppleMusicTrack).where(relaxed).limit(self.MAX_CANDIDATES)
                rows = list((await self.session.execute(stmt)).scalars().all())

        if not rows and len(conds) > 1:  # ancora vuoto: allarga a OR pieno
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

    async def _preload_track_counts(self, rows: Iterable[AppleMusicTrack]) -> None:
        """
        Conta le righe AppleMusicTrack REALI per ciascun collection_id
        candidato, in una singola query aggregata (COUNT ... GROUP BY),
        invece di fidarsi solo di AppleMusicAlbum.track_count.

        Perché serve: track_count su AppleMusicAlbum è scritto SOLO da
        ITunesProvider._persist_lookup_results, quindi è affidabile per
        album con origine iTunes ma può essere assente/stale per album la
        cui unica fonte è stata YouTube (che non fornisce mai un segnale
        testuale di edizione, es. "- Single" nel nome). Contare le righe
        reali già presenti nel DB per quel collection_id è il segnale più
        diretto disponibile, indipendente dal nome dell'album.

        Nota: questo conta solo le tracce GIA' note al DB locale per quel
        album, quindi è un lower-bound, non il track_count "ufficiale" del
        catalogo. È comunque più affidabile di un campo denormalizzato
        assente, e viene usato solo come fallback quando track_count non è
        disponibile (vedi _edition_score).
        """
        ids = {r.collection_id for r in rows if r.collection_id and r.collection_id not in self._real_track_count_cache}
        if not ids:
            return
        stmt = (
            select(AppleMusicTrack.collection_id, func.count(AppleMusicTrack.track_id))
            .where(AppleMusicTrack.collection_id.in_(ids))
            .group_by(AppleMusicTrack.collection_id)
        )
        result = await self.session.execute(stmt)
        for collection_id, count in result.all():
            self._real_track_count_cache[collection_id] = count

    async def _preload_artists(self, rows: Iterable[AppleMusicTrack]) -> None:
        """
        Batch-load di AppleMusicArtists per gli artist_id dei candidati,
        stessa strategia IN(...) di _preload_albums/_preload_track_counts
        (elimina N+1). I miss vengono cachati esplicitamente a None così
        una to_meta() successiva su quello stesso artist_id non riparte
        con una query singola inutile.
        """
        ids = {r.artist_id for r in rows if r.artist_id and r.artist_id not in self._artist_cache}
        if not ids:
            return
        stmt = select(AppleMusicArtist).where(AppleMusicArtist.artist_id.in_(ids))
        found = {a.artist_id: a for a in (await self.session.execute(stmt)).scalars().all()}
        for aid in ids:
            self._artist_cache[aid] = found.get(aid)

    async def _get_artist(self, artist_id: Optional[int]) -> Optional[AppleMusicArtist]:
        """Fallback puntuale per to_meta() quando l'artist_id non è stato pre-caricato in batch (es. hit ISRC a riga singola)."""
        if not artist_id:
            return None
        if artist_id in self._artist_cache:
            return self._artist_cache[artist_id]
        stmt = select(AppleMusicArtist).where(AppleMusicArtist.artist_id == artist_id)
        artist = (await self.session.execute(stmt)).scalar_one_or_none()
        self._artist_cache[artist_id] = artist
        return artist

    @staticmethod
    def _expects_short_form(hint_norm: str, title_norm: str) -> bool:
        if not hint_norm:
            return True
        if FakeAlbumSuffix.has(hint_norm):
            return True
        core_title = strip_parenthetical(title_norm).strip()
        return hint_norm in (title_norm, core_title)

    def _resolve_edition(self, row: AppleMusicTrack, album: AppleMusicAlbum, q: _Query) -> ReleaseEdition:
        """
        Risolve la ReleaseEdition del candidato usando, in ordine di
        preferenza:
        1. AppleMusicAlbum.track_count se > 0 (affidabile: scritto da iTunes).
        2. Conteggio reale delle righe AppleMusicTrack per quel collection_id,
           se track_count manca/è 0 (fallback per album di sola origine YT).

        Il secondo caso produce una ReleaseEdition con track_count_verified
        coerente (vedi ReleaseEdition.from_verified_track_count): non è più
        un UNKNOWN "silenziosamente permissivo", ma un'edizione dedotta da
        un conteggio reale, anche se solo un lower-bound.
        """
        declared_count = album.track_count or 0
        if declared_count > 0:
            return ReleaseEdition.from_collection(
                collection_type=album.collection_type or "",
                collection_name=album.collection_name or "",
                track_count=declared_count,
                title_norm=q.title_norm,
                track_count_verified=True,
            )

        real_count = self._real_track_count_cache.get(row.collection_id, 0)
        if real_count > 0:
            self.log.debug(
                f"[LocalDbMatcher] track_count assente per collection_id={row.collection_id}, "
                f"uso conteggio reale DB={real_count} come fallback."
            )
            return ReleaseEdition.from_verified_track_count(
                track_count=real_count,
                collection_name=album.collection_name or "",
                title_norm=q.title_norm,
            )

        # Nessun segnale affidabile: UNKNOWN esplicito, non verificato.
        # Questo NON deve essere silenziosamente trattato come "va bene",
        # va gestito esplicitamente da chi consuma questa edizione
        # (vedi _edition_score sotto: penalità leggera invece di 0.0).
        return ReleaseEdition.from_collection(
            collection_type=album.collection_type or "",
            collection_name=album.collection_name or "",
            track_count=0,
            title_norm=q.title_norm,
            track_count_verified=False,
        )

    def _edition_score(self, row: AppleMusicTrack, q: _Query) -> float:
        album = self._album_cache.get(row.collection_id) if row.collection_id else None
        if not album:
            return 0.0

        if TextCleaner.normalize(album.artist_name or "") in MusicPatterns.VARIOUS_ARTISTS:
            return -0.5

        edition = self._resolve_edition(row, album, q)
        if edition.kind is ReleaseKind.COMPILATION:
            return -0.5

        album_norm = TextCleaner.normalize(album.collection_name or "")

        # Match album esplicito ha priorità assoluta su tutto il resto.
        if q.hint_norm:
            sim = TextCleaner.album_edition_similarity(q.hint_norm, album_norm)
            if sim >= 0.90:
                return 1.0          # match album quasi esatto: bonus massimo
            if sim >= 0.70:
                return 0.5 + sim * 0.3

        if q.expects_short_form:
            if not edition.is_confident:
                # Non sappiamo con certezza se questo candidato è un
                # singolo o un album (nessun track_count affidabile, nessun
                # conteggio reale > 0, nessun collection_type dal
                # provider). Prima di questo fix questo caso otteneva -0.15
                # (stesso trattamento di "sappiamo che è un album"), un
                # bias implicito contro candidati semplicemente privi di
                # dati — tipicamente le release con unica origine YouTube.
                # Penalità più leggera ma esplicita: meglio di un secco
                # "sappiamo che non è un singolo", ma comunque inferiore al
                # bonus riservato a un singolo CONFERMATO.
                self.log.debug(
                    f"[LocalDbMatcher] edizione non verificata per collection_id={row.collection_id} "
                    f"({edition.describe()}), applico penalità ridotta invece di trattarlo come album."
                )
                return -0.05
            return 0.20 if edition.is_short_form else -0.15

        if q.hint_norm:
            sim = TextCleaner.album_edition_similarity(q.hint_norm, album_norm)
            if edition.is_short_form:
                return -0.25
            return sim * 0.3 - 0.05

        return 0.0

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
            "trackExplicitness": t.track_explicitness.lower(), "discCount": t.disc_count,
            "discNumber": t.disc_number, "trackCount": t.track_count, "trackNumber": t.track_number,
            "trackTimeMillis": t.track_time_millis, "primaryGenreName": t.primary_genre_name,
            "isrc": t.isrc,
        }