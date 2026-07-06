from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from Algorithm.TextCleaner import TextCleaner
from Helpers.MusicBrainzHelper import MusicBrainzHelper
from Helpers.MetaMapper import MetaMapper
from Model.Song import Song
from Pipeline.PipelineContext import PipelineContext
from Pipeline.PipelineResult import ITunesResult, MBResult, MatchConfidence
from Pipeline.ReleaseEdition import ReleaseEdition, isrc_match_is_safe
from Pipeline.VersionGuard import VersionGuard
from Providers.DeezerProvider import DeezerProvider
from Providers.ItunesProvider import ITunesProvider
from Providers.MusicBrainzApi import MusicBrainzApiRequstor
from Algorithm.BestMatch import TrackMatcher
from Providers.SpotifyProvider import SpotifyProvider
from Utils.MusicPatterns import MusicPatterns
from Database.Model.ItunesModel import AppleMusicAlbum, AppleMusicTrack
from Database.Service.TrackService import TrackService
from Database.Service.AlbumService import AlbumService


class MetadataPipeline:
    """
    Orchestratore enrichment per-song.

    Fasi (in ordine, ognuna può fare return anticipato via ctx):
      1. Spotify          → opzionale, fornisce ISRC + artisti accurati
      2. MusicBrainz       → opzionale, recording + release + edizione
      3. DB locale         → se hit, read-only: NESSUN provider remoto
         viene più interrogato e NESSUNA scrittura DB viene fatta per
         questa song (vedi PipelineContext.db_hit_readonly)
      4. Deezer (ISRC)     → enrichment veloce se abbiamo un ISRC
      5. iTunes            → ricerca principale, propaga release_edition
      6. artist_collection → risoluzione featuring/collaboratori
      7. Deezer (fallback) → completa i campi ancora mancanti

    Rispetto alla versione precedente: niente più doppia chiamata a
    _step_local_db, niente più tuple posizionali passate a mano tra step
    (tutto vive in PipelineContext), e ogni punto che tocca un ISRC
    condiviso tra provider verifica la compatibilità di ReleaseEdition
    prima di fondere campi di release (track_number/disc_number/album).

    NUOVO (fix MB/Spotify "Remix non trovato"):
    Quando il titolo originale richiede esplicitamente un tag di versione
    (remix/live/acoustic/ecc.) ma il miglior candidato MB o Spotify trovato
    non lo ha nel proprio titolo, viene tentato UN secondo giro di ricerca
    più permissivo prima di arrendersi:
      - MB:      query con tag di versione incluso nella phrase query
                 (vedi MusicBrainzApiRequstor._build_query include_version_tag).
      - Spotify: nuova ricerca senza il filtro _EXCLUDE_TITLE_RE, accettando
                 esplicitamente candidati con tag di versione nel titolo.
    Se anche il fallback non produce un candidato coerente, il comportamento
    resta quello originale (MBResult/spotify vuoti, fallback su iTunes) —
    nessun dato viene "inventato": si amplia solo il recall della ricerca.
    """

    def __init__(
        self,
        itunes: ITunesProvider,
        mb: MusicBrainzApiRequstor,
        deezer: DeezerProvider,
        logger: Optional[logging.Logger] = None,
        spotify: Optional[SpotifyProvider] = None,
        use_mb: bool = True,
        use_spotify: bool = False,
        always_fallback_cover: bool = False,
        db_session: Optional[AsyncSession] = None,
    ) -> None:
        self.itunes                = itunes
        self.mb                    = mb
        self.deezer                = deezer
        self.spotify               = spotify
        self.use_mb                = use_mb
        self.use_spotify           = use_spotify
        self.always_fallback_cover = always_fallback_cover
        self.log                   = logger or logging.getLogger(__name__)
        self.matcher               = TrackMatcher(min_score=MusicPatterns.MATCHER_MIN_SCORE)
        self.mb.set_matcher(self.matcher)
        self.db_session            = db_session
        self.guard                 = VersionGuard(logger=self.log)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self, song: Song) -> Song:
        song.mark_tagging()
        self.log.info(f"[Pipeline] '{song.meta.title}' - '{song.meta.artist}'")

        ctx = PipelineContext.start(song)

        await self._phase_spotify(ctx)
        await self._phase_musicbrainz(ctx)

        if await self._phase_local_db(ctx):
            # Hit read-only: nessun provider remoto, nessuna scrittura DB.
            return self._finalize_from_db(ctx)

        await self._phase_deezer_isrc(ctx)

        # Secondo tentativo DB SOLO se Deezer ha fornito un ISRC nuovo che
        # non avevamo al primo tentativo (es. ISRC arrivato da Deezer e non
        # da Spotify/MB). Evita la doppia query quando non cambia nulla.
        if ctx.deezer_isrc_result and not ctx.db_hit:
            if await self._phase_local_db(ctx, force=True):
                return self._finalize_from_db(ctx)

        await self._phase_itunes(ctx)
        await self._phase_artist_collection(ctx)
        await self._phase_deezer_fallback(ctx)

        self._finalize_compilation(ctx.song)
        return ctx.song

    # ── Fase 1: Spotify ──────────────────────────────────────────────────────

    async def _phase_spotify(self, ctx: PipelineContext) -> None:
        if not (self.use_spotify and self.spotify and self.spotify.is_active):
            return
        song = ctx.song
        title_has_remix = bool(MusicPatterns.VERSION_TAG_RE.search(song.meta.title))

        try:
            sp_track = self.spotify.search(
                title=song.meta.title, artist=song.meta.artist, album=song.meta.album,
                duration_ms=song.meta.duration_ms, isrc=song.meta.isrc,
            )
        except Exception as exc:
            self.log.debug(f"[Pipeline][Spotify] {exc}")
            sp_track = None

        # NUOVO: se il primo giro non ha trovato nulla (o ha trovato un
        # candidato senza il tag di versione richiesto), e il titolo
        # richiede esplicitamente un tag (remix/live/ecc.), riprova con un
        # secondo giro che accetta candidati col tag — coprendo i casi in
        # cui SpotifyProvider._pick_best ha scartato la traccia corretta
        # tramite _EXCLUDE_TITLE_RE pur essendo quella richiesta.
        if title_has_remix:
            sp_track_name = (sp_track or {}).get("name", "")
            has_tag = bool(MusicPatterns.VERSION_TAG_RE.search(sp_track_name)) if sp_track else False
            if not sp_track or not has_tag:
                try:
                    retry_track = self.spotify.search_allow_version_tag(
                        title=song.meta.title, artist=song.meta.artist, album=song.meta.album,
                        duration_ms=song.meta.duration_ms, isrc="",
                    ) if hasattr(self.spotify, "search_allow_version_tag") else None
                except Exception as exc:
                    self.log.debug(f"[Pipeline][Spotify] retry tag: {exc}")
                    retry_track = None

                if retry_track and MusicPatterns.VERSION_TAG_RE.search(retry_track.get("name", "")):
                    self.log.debug(
                        f"[Pipeline][Spotify] retry con tag versione riuscito: "
                        f"'{retry_track.get('name')}'"
                    )
                    sp_track = retry_track
                elif sp_track and not has_tag:
                    # Il primo giro aveva trovato qualcosa ma senza il tag
                    # richiesto: non fidarsi, è quasi certamente la versione
                    # originale, non la remix/live cercata.
                    self.log.debug(
                        f"[Pipeline][Spotify] match '{sp_track.get('name')}' incoerente "
                        f"col tag di versione richiesto da '{song.meta.title}', scartato."
                    )
                    sp_track = None

        if not sp_track:
            self.log.debug(f"[Pipeline][Spotify] miss: '{ctx.original_title}'")
            return

        sp_mapped = self.spotify.map_to_meta(sp_track)
        ctx.spotify_mapped = sp_mapped
        ctx.spotify_isrc = sp_mapped.get("isrc", "")
        self.log.info(f"[Pipeline][Spotify] '{sp_mapped.get('title')}' ISRC={ctx.spotify_isrc!r}")
        song.meta.apply(sp_mapped, overwrite_keys={"artist_collection", "artist"})

    # ── Fase 2: MusicBrainz ──────────────────────────────────────────────────

    async def _phase_musicbrainz(self, ctx: PipelineContext) -> None:
        if not self.use_mb:
            return

        mb_result = await self._resolve_mb(ctx.song)
        ctx.mb_result = mb_result
        song = ctx.song

        if mb_result.found:
            await self._apply_mb_country(mb_result.recording)
            song.meta.set_if_empty("isrc", mb_result.isrc)
            self.log.debug(f"[Pipeline] MB confidence={mb_result.confidence.name} edition={mb_result.release_edition.describe() if mb_result.release_edition else 'n/a'}")
        else:
            self.log.warning(f"[Pipeline] MB miss: '{ctx.original_title}'")

        ctx.mb_track_disc = self._resolve_mb_track_disc(song, mb_result)

    async def _resolve_mb(self, song: Song) -> MBResult:
        raw_title  = song.meta.title.strip()
        raw_artist = song.meta.artist.strip()
        raw_album  = song.meta.album.strip()
        isrc_hint  = song.meta.isrc.strip()
        duration   = song.meta.duration_ms

        clean_artist    = TextCleaner.clean_text(raw_artist, field_type="artist")
        clean_title     = TextCleaner.clean_text(raw_title, artist=clean_artist, field_type="title")
        clean_album     = TextCleaner.normalize(raw_album)
        title_has_remix = bool(MusicPatterns.VERSION_TAG_RE.search(raw_title))

        best_score, best_rec_raw = -1.0, None
        # True solo se il match vincente proviene da un fetch_by_isrc riuscito
        # E coerente col tag di versione del titolo. Determina se la
        # confidence finale può legittimamente essere ISRC_EXACT.
        isrc_fetch_trusted = False

        if isrc_hint:
            rec = await self.mb.fetch_by_isrc(isrc_hint)
            if rec and self._isrc_fetch_is_trustworthy(rec, raw_title, title_has_remix):
                score = self.matcher.score_candidate(
                    title=clean_title, artist=clean_artist, album_hint=clean_album,
                    duration_ms=duration, isrc=isrc_hint,
                    candidate=self.mb._recording_to_candidate(rec),
                )
                if score and score > best_score:
                    best_score, best_rec_raw, isrc_fetch_trusted = score, rec, True
            elif rec:
                self.log.debug(
                    f"[Pipeline] ISRC hint {isrc_hint!r} scartato: titolo MB "
                    f"{self.mb._recording_title(rec)!r} incoerente col tag di "
                    f"versione di '{raw_title}' (probabile ISRC errato a monte, "
                    f"es. da yt-dlp/YouTube Music legato all'originale invece "
                    f"che alla versione richiesta)."
                )

        # Ricerca testuale: SEMPRE eseguita se il fetch ISRC non ha prodotto
        # un match affidabile, anche se isrc_hint era presente. In precedenza
        # un isrc_hint "valido ma incoerente" saltava del tutto questo passo.
        if not isrc_fetch_trusted:
            query = self.mb._build_query(raw_title, raw_artist, raw_album)
            for rec in await self.mb._search_recordings(query):
                score = self.matcher.score_candidate(
                    title=clean_title, artist=clean_artist, album_hint=clean_album,
                    duration_ms=duration, isrc=isrc_hint,
                    candidate=self.mb._recording_to_candidate(rec),
                )
                if score and score > best_score:
                    best_score, best_rec_raw = score, rec

            # NUOVO: se il titolo richiede un tag di versione esplicito ma il
            # miglior candidato trovato non lo ha nel proprio titolo MB,
            # tenta un secondo giro con la query che include il tag come
            # parte della phrase query. Accetta SOLO candidati che abbiano
            # davvero il tag nel titolo, per non sostituire un match
            # legittimo con uno peggiore solo perché "ha il tag per caso".
            best_title_now = self.mb._recording_title(best_rec_raw) if best_rec_raw else ""
            if title_has_remix and not MusicPatterns.VERSION_TAG_RE.search(best_title_now):
                tagged_query = self.mb._build_query(
                    raw_title, raw_artist, raw_album, include_version_tag=True
                )
                if tagged_query != query:
                    for rec in await self.mb._search_recordings(tagged_query):
                        cand_title = self.mb._recording_title(rec)
                        if not MusicPatterns.VERSION_TAG_RE.search(cand_title):
                            continue
                        score = self.matcher.score_candidate(
                            title=clean_title, artist=clean_artist, album_hint=clean_album,
                            duration_ms=duration, isrc=isrc_hint,
                            candidate=self.mb._recording_to_candidate(rec),
                        )
                        if score and score > best_score:
                            best_score, best_rec_raw = score, rec
                            self.log.debug(
                                f"[Pipeline][MB] fallback con tag versione riuscito: "
                                f"'{cand_title}' score={score:.2f}"
                            )

        if not best_rec_raw or best_score < 0.35:
            return MBResult()

        recording = await self.mb.fetch_recording_by_id(
            best_rec_raw["id"],
            inc_params="releases+media+artist-credits+isrcs+release-groups+tags+genres",
        )
        if not recording:
            return MBResult()

        final_isrc   = (recording.get("isrcs") or [""])[0] or (best_rec_raw.get("isrcs") or [""])[0]
        confidence   = self._mb_confidence(
            best_score, final_isrc, isrc_hint, duration, recording.get("length"),
            isrc_fetch_trusted=isrc_fetch_trusted,
        )
        releases     = recording.get("releases", [])
        best_release = self._pick_best_release(releases, clean_album, raw_title)
        album_score  = 0.0

        if clean_album and best_release:
            album_score = TextCleaner.album_edition_similarity(
                clean_album,
                TextCleaner.clean_text(best_release.get("title", ""), field_type="album"),
            )

        release_edition = (
            self.mb.edition_for_release(best_release, title_norm=TextCleaner.normalize(clean_title))
            if best_release else None
        )

        return MBResult(
            recording=recording,
            album=best_release,
            track_score=best_score,
            album_score=album_score,
            confidence=confidence,
            isrc=final_isrc,
            album_is_deluxe=bool(MusicPatterns.DELUXE_TAG_RE.search((best_release or {}).get("title", ""))),
            title_has_remix=title_has_remix,
            release_edition=release_edition,
        )

    def _isrc_fetch_is_trustworthy(self, rec: Dict, original_title: str, title_has_remix: bool) -> bool:
        """
        Valida che la recording ottenuta da fetch_by_isrc sia coerente col
        tag di versione del titolo cercato, prima di fidarsi ciecamente
        dell'ISRC fornito a monte (yt-dlp/YouTube Music spesso lega un
        video remix all'ISRC del brano originale, o viceversa).

        Regola, simmetrica a quella già usata in MusicBrainzApiRequstor
        per le alt-version (instrumental/karaoke/ecc.):
        - Se il titolo originale richiede un tag di versione (remix/live/
          acoustic/...) e la recording MB risultante NON lo ha nel titolo,
          l'ISRC è sospetto → non fidarsi.
        - Il contrario non è penalizzato: un ISRC che porta a una recording
          con tag di versione quando il titolo originale non lo richiedeva
          esplicitamente resta accettabile (capita che MB disambiguifichi
          più di quanto serva).
        """
        if not title_has_remix:
            return True
        cand_title = self.mb._recording_title(rec)
        return bool(MusicPatterns.VERSION_TAG_RE.search(cand_title))

    def _pick_best_release(self, releases: list, clean_album: str, original_title: str) -> Dict:
        if not releases:
            return {}
        wants_alt  = MusicPatterns.is_alt_version(original_title)
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

    # ── Fase 3: DB locale ────────────────────────────────────────────────────

    async def _phase_local_db(self, ctx: PipelineContext, force: bool = False) -> bool:
        """
        Popola ctx.db_hit/db_hit_readonly se trova un match locale.
        Ritorna True se la pipeline deve fermarsi qui (DB hit valido).

        `force=True` permette un secondo tentativo dopo che Deezer ha
        fornito un nuovo ISRC, ma solo se non avevamo già un hit.
        """
        if not self.db_session or (ctx.db_hit and not force):
            return bool(ctx.db_hit_readonly and ctx.db_hit)

        isrc = ctx.search_isrc
        song = ctx.song
        try:
            db_hit = await self._lookup_local_db(song, isrc)
        except Exception as exc:
            self.log.debug(f"[Pipeline][DB] query fallita: {exc}")
            db_hit = {}

        if not db_hit:
            return False

        ctx.db_hit = db_hit
        ctx.db_hit_readonly = True
        self.log.info("[Pipeline][DB] Hit locale → read-only, salto Deezer + iTunes.")
        return True

    async def _lookup_local_db(self, song: Song, isrc: str) -> Dict[str, Any]:
        title_has_remix = bool(MusicPatterns.VERSION_TAG_RE.search(song.meta.title))

        if isrc:
            track = await self._db_track_by_isrc(isrc, song.meta.album, song.meta.title)
            if track and self._db_track_matches_version(track, title_has_remix):
                return await self._db_track_to_meta(track)
            if track:
                self.log.debug(
                    f"[Pipeline][DB] ISRC {isrc!r} trovato ma titolo DB "
                    f"{track.track_name!r} incoerente col tag di versione "
                    f"richiesto da '{song.meta.title}' — scartato, ricado "
                    f"sul fallback per titolo/artista."
                )

        title_norm  = TextCleaner.normalize(song.meta.title)
        artist_norm = TextCleaner.normalize(TextCleaner.primary_artist(song.meta.artist))
        if not title_norm:
            return {}

        track = await self._db_track_by_title_artist(
            title_norm, artist_norm, song.meta.album, song.meta.duration_ms,
            )
        if track and not self._db_track_matches_version(track, title_has_remix):
            self.log.debug(
                f"[Pipeline][DB] Match per titolo/artista {track.track_name!r} "
                f"incoerente col tag di versione richiesto, scartato."
            )
            return {}
        return await self._db_track_to_meta(track) if track else {}

    @staticmethod
    def _db_track_matches_version(track: AppleMusicTrack, title_has_remix: bool) -> bool:
        """
        Stessa regola applicata in _isrc_fetch_is_trustworthy per MB: se il
        titolo cercato richiede esplicitamente un tag di versione (remix/
        live/acoustic/ecc.), la riga DB candidata deve averlo nel proprio
        nome traccia. Il contrario non è penalizzato.
        """
        if not title_has_remix:
            return True
        return bool(MusicPatterns.VERSION_TAG_RE.search(track.track_name or ""))

    async def _db_track_by_isrc(
        self, isrc: str, album_hint: str = "", title_hint: str = "",
    ) -> Optional[AppleMusicTrack]:
        """
        Cerca tracce con lo stesso ISRC. Se ce ne sono più di una (caso
        comune: stesso recording uscito come single E poi su un album, O
        — caso concretamente osservato — l'originale e una remix/edizione
        con featuring che condividono l'ISRC, es. "Leaked" e "Leaked
        (feat. Lil Wayne) [Remix]" entrambe TrackId distinti nello stesso
        album con lo stesso ISRC), NON sceglie semplicemente quella con
        più tracce nell'album o con miglior similarity sull'album: usa
        PRIMA la coerenza del track_name col title_hint (incluso il tag
        di versione, remix/live/acoustic/ecc.) come filtro decisivo, e
        SOLO come tie-break secondario considera ReleaseEdition/album.

        FIX (bug riportato): la versione precedente calcolava `_score`
        usando esclusivamente album_sim + edition_bonus, MAI confrontando
        track.track_name con title_hint. Con due righe sullo stesso ISRC
        appartenenti allo STESSO album (stesso album_sim, stesso
        edition_bonus), `max()` su punteggi identici restituiva una riga
        arbitraria (ordine di iterazione) — risultato osservato: veniva
        scartata la riga "Leaked (feat. Lil Wayne) [Remix]" già presente
        nel DB in favore di "Leaked", nonostante il titolo richiesto
        ('Leaked (Remix)') richiedesse esplicitamente il tag versione, che
        il chiamante (_lookup_local_db → _db_track_matches_version)
        verificava SOLO dopo aver già fissato la scelta sbagliata.
        """
        stmt = select(AppleMusicTrack).where(AppleMusicTrack.isrc == isrc.upper())
        result = await self.db_session.execute(stmt)
        rows: list[AppleMusicTrack] = list(result.scalars().all())

        if not rows:
            return None
        if len(rows) == 1:
            return rows[0]

        title_norm = TextCleaner.normalize(title_hint) if title_hint else ""
        title_has_remix = bool(MusicPatterns.VERSION_TAG_RE.search(title_hint)) if title_hint else False

        # ── Step 1 (decisivo): coerenza tag di versione sul track_name ──────
        # Se il titolo cercato richiede esplicitamente un tag (remix/live/
        # acoustic/...), le righe che lo hanno nel proprio track_name vanno
        # SEMPRE preferite a quelle che non lo hanno — indipendentemente da
        # album/edition. Se nessuna riga ha il tag (caso comune: l'utente
        # non lo richiede, o il DB non distingue versioni), si passa pari
        # a tutte le righe allo step 2.
        if title_has_remix:
            tagged_rows = [
                r for r in rows
                if MusicPatterns.VERSION_TAG_RE.search(r.track_name or "")
            ]
            if tagged_rows:
                rows = tagged_rows
            # se nessuna riga ha il tag, non scartiamo: lasciamo che il
            # chiamante (_db_track_matches_version) decida a valle, ma
            # almeno la selezione tra le righe rimanenti userà ancora la
            # similarity testuale allo step 1b qui sotto.
        if title_norm:
            # ── Step 1b: anche senza tag obbligatorio, preferisci la riga
            # col track_name testualmente più simile al titolo cercato.
            # Disambigua i casi (anche senza remix) in cui due righe stesso
            # ISRC hanno titoli leggermente diversi (es. edit/clean/etc.).
            sim_scored = [
                (TextCleaner.title_similarity(title_norm, TextCleaner.normalize(r.track_name or "")), r)
                for r in rows
            ]
            best_sim = max(s for s, _ in sim_scored)
            # Mantieni solo le righe entro una tolleranza ristretta dal best,
            # per non perdere il tie-break su album/edition tra candidati
            # comunque validi (es. due righe identiche su album diversi).
            rows = [r for s, r in sim_scored if s >= best_sim - 0.05]
            if len(rows) == 1:
                return rows[0]

        # ── Step 2: tie-break su album/edition tra le righe rimaste ─────────
        album_map: dict[int, AppleMusicAlbum] = {}
        edition_map: dict[int, ReleaseEdition] = {}
        for row in rows:
            if row.collection_id and row.collection_id not in album_map:
                album = await AlbumService.get(self.db_session, row.collection_id)
                if album:
                    album_map[row.collection_id] = album
                    edition_map[row.collection_id] = ReleaseEdition.from_collection(
                        collection_type=album.collection_type or "",
                        collection_name=album.collection_name or "",
                        track_count=album.track_count or 0,
                        title_norm=title_norm,
                    )

        hint_norm = TextCleaner.normalize(album_hint) if album_hint else ""
        hint_edition = (
            ReleaseEdition.from_collection(collection_name=album_hint, title_norm=title_norm)
            if album_hint else None
        )

        def _score(track: AppleMusicTrack) -> float:
            album = album_map.get(track.collection_id or 0)
            edition = edition_map.get(track.collection_id or 0)
            if not album:
                return 0.0

            album_sim = (
                TextCleaner.title_similarity(hint_norm, TextCleaner.normalize(album.collection_name or ""))
                if hint_norm else 0.0
            )

            # Coerenza di edizione con l'hint: se l'utente cercava esplicitamente
            # un single (o non ha hint_album), non penalizzare un single solo
            # perché ha meno tracce; se invece l'hint è un album, il bonus va
            # alle release con più tracce SOLO se l'edizione è compatibile.
            edition_bonus = 0.0
            if hint_edition and edition:
                if hint_edition.compatible_with(edition):
                    edition_bonus = min((album.track_count or 1) / 20.0, 0.3)
                else:
                    edition_bonus = -0.5  # penalità: edizione richiesta diversa
            elif edition and not edition.is_short_form:
                # nessun hint: leggera preferenza per l'album solo come tie-break
                edition_bonus = min((album.track_count or 1) / 40.0, 0.15)

            return album_sim + edition_bonus

        return max(rows, key=_score)

    async def _db_track_by_title_artist(
        self, title_norm: str, artist_norm: str
    ) -> Optional[AppleMusicTrack]:
        try:
            stmt        = select(AppleMusicTrack).limit(200)
            rows_result = await self.db_session.execute(stmt)
            rows: list[AppleMusicTrack] = list(rows_result.scalars().all())
        except Exception as exc:
            self.log.debug(f"[Pipeline][DB] select tracks fallito: {exc}")
            return None

        best_track: Optional[AppleMusicTrack] = None
        best_score = 0.0

        for row in rows:
            t_norm = TextCleaner.normalize(row.track_name or "")
            a_norm = TextCleaner.normalize(row.artist_name or "")

            t_sim = TextCleaner.title_similarity(title_norm, t_norm)
            if t_sim < 0.88:
                continue

            a_sim = TextCleaner.title_similarity(artist_norm, a_norm) if artist_norm else 1.0
            if a_sim < 0.70:
                continue

            combined = 0.6 * t_sim + 0.4 * a_sim
            if combined > best_score:
                best_score = combined
                best_track = row

        if best_track:
            self.log.debug(f"[Pipeline][DB] Hit: '{best_track.track_name}' score={best_score:.2f}")
        return best_track

    async def _db_track_to_meta(self, track: AppleMusicTrack) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "title":                track.track_name or "",
            "artist":               track.artist_name or "",
            "album":                track.collection_name or "",
            "track_number":         track.track_number or 0,
            "disc_number":          track.disc_number or 0,
            "explicit":             (track.track_explicitness or "").lower() == "explicit",
            "genre":                track.primary_genre_name or "",
            "itunes_track_id":      str(track.track_id) if track.track_id else "",
            "itunes_artist_id":     str(track.artist_id) if track.artist_id else "",
            "itunes_collection_id": str(track.collection_id) if track.collection_id else "",
        }

        if track.artwork_url:
            meta["cover_url"] = re.sub(r"\d+x\d+bb", "3000x3000bb", track.artwork_url)

        if track.collection_id and self.db_session:
            try:
                album = await AlbumService.get(self.db_session, track.collection_id)
                if album:
                    meta["album_artist"] = album.artist_name or track.artist_name or ""
                    if album.release_date:
                        meta["year"] = str(album.release_date.year)
                    if not meta["genre"] and album.primary_genre_name:
                        meta["genre"] = album.primary_genre_name
            except Exception as exc:
                self.log.debug(f"[Pipeline][DB] album join fallito: {exc}")

        return {k: v for k, v in meta.items() if v not in (None, "", 0, False)}

    def _finalize_from_db(self, ctx: PipelineContext) -> Song:
        """
        Applica il DB-hit e chiude la pipeline. Nessuna chiamata a provider
        remoti, nessuna scrittura: la song è già "vera" così com'è in DB.
        """
        song = ctx.song
        song.meta.apply(ctx.db_hit, overwrite_keys=set(ctx.db_hit.keys()))
        if ctx.mb_track_disc:
            self._apply_mb_track_disc(song, ctx.mb_track_disc)
        self._finalize_compilation(song)
        # artist_collection risolto via Spotify/MB se disponibili, senza toccare iTunes/DB
        ctx.accurate_artists = (ctx.spotify_mapped or {}).get("artist_collection", "")
        self._finalize_artist_collection(
            song, ctx.accurate_artists, itunes_artist_raw=ctx.db_hit.get("artist", ""),
            raw_title=song.raw.get("title", ""),
        )
        self._finalize_compilation(song)
        return song

    # ── Fase 4: Deezer ISRC ──────────────────────────────────────────────────

    async def _phase_deezer_isrc(self, ctx: PipelineContext) -> None:
        isrc = ctx.search_isrc
        if not isrc:
            await self._phase_deezer_no_isrc_early(ctx)
            return

        try:
            raw = await self.deezer.get_by_isrc(isrc)
        except Exception as exc:
            self.log.debug(f"[Pipeline][Deezer-ISRC] {exc}")
            raw = {}

        if not raw:
            self.log.debug(f"[Pipeline][Deezer-ISRC] ISRC {isrc} non trovato su Deezer")
            return

        mapped = MetaMapper.from_deezer_isrc(raw, logger=self.log)
        ctx.deezer_isrc_result = mapped
        self.log.debug(f"[Pipeline][Deezer-ISRC] '{mapped.get('title')}' genre={mapped.get('genre')!r}")

        # FIX (refactoring): track_number/disc_number sono campi specifici
        # della RELEASE, non del recording. Un ISRC può essere condiviso tra
        # l'originale e un remix (visto concretamente: "Brothers" e
        # "Brothers (feat. Lil Durk) [Remix]" hanno lo stesso ISRC su
        # iTunes/Deezer) — in quel caso Deezer restituisce il titolo e la
        # posizione-traccia dell'edizione che ha indicizzato (spesso
        # l'originale), non necessariamente quella richiesta. Applicarli
        # incondizionatamente sovrascriveva un track_number corretto (o
        # ancora da determinare via iTunes) con quello sbagliato
        # dell'altra edizione. cover_url/explicit/genre/year restano sempre
        # sicuri: sono tipicamente coerenti anche tra edizioni diverse dello
        # stesso recording.
        title_has_remix = bool(MusicPatterns.VERSION_TAG_RE.search(ctx.song.meta.title))
        deezer_title_ok = (
            not title_has_remix
            or bool(MusicPatterns.VERSION_TAG_RE.search(mapped.get("title", "")))
        )

        overwrite = {"cover_url", "explicit", "genre", "year"}
        if deezer_title_ok:
            overwrite |= {"track_number", "disc_number"}
        else:
            self.log.debug(
                f"[Pipeline][Deezer-ISRC] track_number/disc_number NON applicati: "
                f"titolo Deezer {mapped.get('title')!r} incoerente col tag di "
                f"versione richiesto da '{ctx.song.meta.title}'"
            )

        ctx.song.meta.apply(mapped, overwrite_keys=overwrite)
        self.log.info("[Pipeline][Deezer-ISRC] Applicato.")

    async def _phase_deezer_no_isrc_early(self, ctx: PipelineContext) -> None:
        """Senza ISRC: avvia subito il fallback completo Deezer (cover/genere/ecc)."""
        await self._step_deezer(ctx.song, ctx.original_title, itunes_found=False, mb_found=ctx.mb_result.found)

    # ── Fase 5: iTunes ───────────────────────────────────────────────────────

    async def _phase_itunes(self, ctx: PipelineContext) -> None:
        song = ctx.song
        mb_result = ctx.mb_result
        search_album = self._resolve_search_album(song, mb_result)
        itunes_min   = 0.70 if mb_result.confidence == MatchConfidence.LOW else MusicPatterns.MATCHER_MIN_SCORE

        itunes_result = await self._search_itunes(
            song, override_title=ctx.original_title or mb_result.mb_title,
            override_album=search_album, override_isrc=ctx.search_isrc, min_score=itunes_min,
        )
        ctx.itunes_result = itunes_result

        if itunes_result.found:
            self._apply_itunes(song, itunes_result, mb_result, ctx.mb_track_disc, ctx.original_title, ctx.original_duration_ms)
            await self._fix_bad_year(song, mb_result)
            if ctx.search_isrc and itunes_result.data.get("itunes_track_id"):
                await self.itunes.persist_track_isrc(
                    int(itunes_result.data["itunes_track_id"]), ctx.search_isrc
                )
        else:
            self.log.warning(f"[Pipeline] iTunes miss: '{ctx.original_title}'")
            if not ctx.deezer_isrc_result and ctx.spotify_mapped:
                song.meta.apply(ctx.spotify_mapped)
            elif not ctx.deezer_isrc_result and self.use_mb:
                self._apply_mb_fallback(song, mb_result)

    async def _search_itunes(
        self, song: Song, override_title: str = "", override_album: str = "",
        override_isrc: str = "", min_score: float = MusicPatterns.MATCHER_MIN_SCORE,
    ) -> ITunesResult:
        title    = (override_title or song.meta.title).strip()
        artist   = song.meta.artist.strip()
        album    = (override_album or song.meta.album).strip()
        isrc     = (override_isrc or song.meta.isrc).strip()
        duration = song.meta.duration_ms

        if not title:
            return ITunesResult()

        try:
            raw = await self.itunes.search(
                title=title, song=song, artist=artist, hint_album=album,
                duration_ms=duration, min_score=min_score,
            )
        except Exception as exc:
            self.log.warning(f"[Pipeline][iTunes] {exc}")
            return ITunesResult()

        if not raw:
            return ITunesResult()

        matched_by_isrc = bool(isrc and raw.get("isrc", "").upper() == isrc.upper())
        edition = raw.pop("_release_edition", None)
        return self._build_itunes_result(raw, matched_by_isrc=matched_by_isrc, title=title, artist=artist, edition=edition)

    def _build_itunes_result(
        self, raw, matched_by_isrc=False, title="", artist="", edition: Optional[ReleaseEdition] = None,
    ) -> ITunesResult:
        confidence = (
            MatchConfidence.ISRC_EXACT if matched_by_isrc
            else self._itunes_confidence(raw, title, artist)
        )
        return ITunesResult(
            data=raw, confidence=confidence, matched_by_isrc=matched_by_isrc,
            itunes_track_number=raw.get("track_number"), itunes_disc_number=raw.get("disc_number"),
            itunes_duration_ms=raw.get("track_time_ms") or raw.get("duration_ms"),
            itunes_album=raw.get("album", ""), release_edition=edition,
        )

    # ── Fase 6: artist_collection ────────────────────────────────────────────

    async def _phase_artist_collection(self, ctx: PipelineContext) -> None:
        song = ctx.song
        accurate_artists = await self._resolve_accurate_artists(song, ctx.spotify_mapped, ctx.deezer_isrc_result, ctx.original_title)
        ctx.accurate_artists = accurate_artists
        itunes_artist_raw = ctx.itunes_result.data.get("artist", "") if ctx.itunes_result.found else ""
        self._finalize_artist_collection(song, accurate_artists, itunes_artist_raw, raw_title=song.raw.get("title", ""))

    # ── Fase 7: Deezer fallback ──────────────────────────────────────────────

    async def _phase_deezer_fallback(self, ctx: PipelineContext) -> None:
        await self._step_deezer(
            ctx.song, ctx.original_title, ctx.itunes_result.found,
            ctx.mb_result.found or bool(ctx.deezer_isrc_result),
        )
        self._finalize_compilation(ctx.song)

    async def _step_deezer(self, song: Song, original_title: str, itunes_found: bool, mb_found: bool) -> None:
        m = song.meta
        if not itunes_found and not mb_found:
            self.log.warning(f"[Pipeline][Deezer] Full fallback: '{original_title}'")
            try:
                raw = await self.deezer.get_full_metadata(title=original_title, artist=m.artist, album=m.album)
                if raw:
                    song.meta.apply(MetaMapper.from_deezer(raw, logger=self.log))
            except Exception as exc:
                self.log.debug(f"[Pipeline][Deezer] full_metadata: {exc}")

        if self.always_fallback_cover or "mzstatic.com/image" not in m.cover_url:
            try:
                url = await self.deezer.get_cover_url(title=m.title, artist=m.artist, album=m.album)
                if url:
                    m.cover_url = url
            except Exception as exc:
                self.log.debug(f"[Pipeline][Deezer] cover: {exc}")

        partial: Dict[str, Any] = {}
        needs_track_disc = m.track_number == 0 or m.disc_number == 0
        needs_genre      = not m.genre

        async def _fetch_track_disc():
            if not needs_track_disc:
                return
            try:
                dt, dd = await self.deezer.get_track_and_disc(title=m.title, artist=m.artist, album=m.album)
                if dt > 0 and m.track_number == 0:
                    partial["track_number"] = dt
                if dd > 0 and m.disc_number == 0:
                    partial["disc_number"] = dd
            except Exception as exc:
                self.log.debug(f"[Pipeline][Deezer] track/disc: {exc}")

        async def _fetch_genre():
            if not needs_genre:
                return
            try:
                g = await self.deezer.get_genre(title=m.title, artist=m.artist)
                if g:
                    partial["genre"] = g
            except Exception as exc:
                self.log.debug(f"[Pipeline][Deezer] genre: {exc}")

        if needs_track_disc or needs_genre:
            await asyncio.gather(_fetch_track_disc(), _fetch_genre())

        if partial:
            song.meta.apply(partial)
        if not m.year:
            raw_year = song.raw.get("year", "")
            if raw_year:
                m.year = str(raw_year)

    # ── Apply helpers ─────────────────────────────────────────────────────────

    def _apply_itunes(self, song, itunes, mb, mb_track_disc, original_title, original_duration) -> None:
        overwrite   = self.guard.safe_overwrite_fields(itunes, mb, original_title, original_duration)
        itunes_data = dict(itunes.data)
        for key in ("track_number", "disc_number"):
            if mb_track_disc.get(key):
                itunes_data.pop(key, None)
                overwrite.discard(key)

        title_will_change = "title" in overwrite and itunes_data.get("title")
        song.meta.apply(itunes_data, overwrite_keys=overwrite)

        if title_will_change:
            # sort_title era già stato derivato dal vecchio titolo (seed) in
            # SongProcessor._postprocess_meta; va rigenerato qui per restare
            # coerente col titolo appena sovrascritto, altrimenti l'ordinamento
            # in Apple Music/iTunes resta legato al titolo "povero" originale.
            song.meta.sort_title = Song.build_sort_name(song.meta.title)
            self.log.debug(f"[Pipeline] title arricchito da iTunes: {song.meta.title!r} (sort_title aggiornato)")

        self.log.info("[Pipeline] iTunes applicato.")

    def _apply_mb_track_disc(self, song: Song, mb_track_disc: Dict[str, int]) -> None:
        for key in ("disc_number", "track_number"):
            val = mb_track_disc.get(key)
            if val:
                setattr(song.meta, key, val)
                self.log.debug(f"[Pipeline] {key} forzato da MB = {val}")

    def _apply_mb_fallback(self, song: Song, mb: MBResult) -> None:
        if not mb.found:
            return
        song.meta.apply(MetaMapper.from_mb_recording(mb.recording, mb.album, logger=self.log))
        detail     = mb.album or mb.recording
        song_title = TextCleaner.clean_text(song.meta.title, field_type="title")
        song_album = TextCleaner.clean_text(song.meta.album, field_type="album")
        first_media, first_track, all_tracks = MusicBrainzHelper._resolve_media_and_track(
            detail, song_title, song_album, self.log
        )
        song.meta.apply(MetaMapper.from_mb_track(first_media, first_track, all_tracks, logger=self.log))
        MusicBrainzHelper.apply_exclusive(song, mb.recording, logger=self.log)
        if not song.meta.album_artist and mb.album:
            credits = mb.album.get("artist-credit", [])
            aa = "".join(
                ac.get("name", "") + ac.get("joinphrase", "")
                for ac in credits if isinstance(ac, dict)
            ).strip()
            if aa:
                song.meta.album_artist = aa
        if not song.meta.label and mb.album:
            label_info = mb.album.get("label-info", [])
            if label_info:
                label = label_info[0].get("label", {}).get("name", "")
                if label:
                    song.meta.label = label

    # ── Resolve helpers ───────────────────────────────────────────────────────

    def _resolve_mb_track_disc(self, song: Song, mb: MBResult) -> Dict[str, int]:
        if not mb.found or mb.confidence < MatchConfidence.GOOD:
            return {}

        # FIX (refactoring): track_number/disc_number sono specifici della
        # release MB risolta. Se il titolo cercato richiede un tag di
        # versione (remix/live/ecc.) ma la recording MB trovata non lo ha
        # nel titolo, MB ha quasi certamente risolto l'edizione sbagliata
        # (visto concretamente: ISRC condiviso tra "Brothers" e "Brothers
        # (feat. Lil Durk) [Remix]" — MB trova l'originale con confidence
        # GOOD/HIGH ma la sua posizione-traccia appartiene a quell'edizione,
        # non al remix). In quel caso non ci si può fidare della posizione:
        # meglio lasciare il campo non forzato e lasciare che iTunes (più
        # specifico sul tag versione, grazie al guard in VersionGuard)
        # fornisca il valore corretto.
        if mb.title_has_remix and not MusicPatterns.VERSION_TAG_RE.search(mb.mb_title or ""):
            self.log.debug(
                f"[Pipeline] MB track/disc NON forzati: recording MB "
                f"{mb.mb_title!r} incoerente col tag di versione richiesto "
                f"da '{song.meta.title}'"
            )
            return {}

        detail = mb.album or mb.recording
        if not detail:
            return {}
        first_media, first_track, all_tracks = MusicBrainzHelper._resolve_media_and_track(
            detail,
            TextCleaner.clean_text(song.meta.title, field_type="title"),
            TextCleaner.clean_text(song.meta.album, field_type="album"),
            self.log,
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
        if mb.mb_album_title and MusicPatterns.DELUXE_TAG_RE.search(original):
            return original
        return mb.mb_album_title or original

    async def _resolve_accurate_artists(
        self, song: Song, sp_mapped: dict,
        deezer_isrc_result: Optional[Dict[str, Any]] = None,
        original_title: str = "",
    ) -> str:
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
        try:
            artists, _ = await self.mb.get_accurate_artists_by_isrc(
                song.meta.isrc, original_title=original_title or song.meta.title,
            )
            return artists
        except Exception as exc:
            self.log.debug(f"[Pipeline] resolve_mb_artists: {exc}")
            return ""

    async def _apply_mb_country(self, recording: Dict) -> None:
        artist = next(
            (
                ac.get("artist", {}).get("name", "") or ac.get("name", "")
                for ac in recording.get("artist-credit", [])
                if isinstance(ac, dict)
            ),
            "",
        )
        if not artist:
            return
        try:
            country = await self.mb._resolve_artist_country(artist)
            target  = country.upper() if country and country.upper() in MusicPatterns.ITUNES_VALID_COUNTRIES else "US"
            self.itunes.set_country_for_artist(target)
            if target == "US":
                self.log.debug(f"[Pipeline] country '{country}' non valida, uso US")
        except Exception as exc:
            self.log.debug(f"[Pipeline] country resolve: {exc}")
            self.itunes.set_country_for_artist("US")

    # ── Finalizzazione ────────────────────────────────────────────────────────

    def _finalize_artist_collection(
        self, song: Song, accurate_artists: str,
        itunes_artist_raw: str = "", raw_title: str = "",
    ) -> None:
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
        m       = song.meta
        norm_aa = TextCleaner.normalize(m.album_artist)
        norm_pa = TextCleaner.normalize(m.artist_collection or m.artist)
        is_various = norm_aa in MusicPatterns.VARIOUS_ARTISTS
        m.compilation = is_various or bool(norm_pa and norm_aa and norm_pa != norm_aa)

    # ── Anno ──────────────────────────────────────────────────────────────────

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
                        self.log.info(f"[Pipeline] Anno: {mb_year} (era {current})")
                        song.meta.year = str(mb_year)
                        return
                    if 1900 <= mb_year <= 2100 and not (year and year.isdigit()):
                        song.meta.year = str(mb_year)
                        return
        if not year or not year.isdigit() or not (1900 <= int(year) <= 2100):
            self.log.warning(f"[Pipeline] Anno errato ({year!r}), recupero da MB")
            song.meta.year = ""
            if not mb.found or not mb.recording:
                return
            rec_id = mb.recording.get("id")
            if not rec_id:
                return
            try:
                rec = await self.mb.fetch_recording_by_id(rec_id, inc_params="releases+release-groups")
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
                            self.log.info(f"[Pipeline] Anno corretto: {y}")
                            return
            except Exception as exc:
                self.log.debug(f"[Pipeline] fix_bad_year: {exc}")

    # ── Score helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _mb_confidence(
        score, final_isrc, isrc_hint, duration_ms, cand_ms, isrc_fetch_trusted: bool = True,
    ) -> MatchConfidence:
        """
        FIX (refactoring): ISRC_EXACT veniva assegnato ogni volta che
        final_isrc == isrc_hint come stringhe, indipendentemente da come il
        candidato era stato trovato. Questo permetteva a un ISRC fornito a
        monte (yt-dlp/YouTube Music) ma incoerente con la versione richiesta
        — es. ISRC dell'originale "Brothers" passato per il video
        "Brothers (Remix)" — di propagarsi come "match certo" anche quando
        il fetch diretto per quell'ISRC era già stato scartato per
        incoerenza di titolo, e il risultato finale arrivava invece dalla
        ricerca testuale. `isrc_fetch_trusted` (default True per i chiamanti
        che non lo passano esplicitamente, retrocompatibile) deve essere
        False in quel caso, impedendo una ISRC_EXACT "fortuita" e facendo
        ricadere la confidence sullo score testuale reale.
        """
        if isrc_fetch_trusted and final_isrc and isrc_hint and final_isrc.upper() == isrc_hint.upper():
            return MatchConfidence.ISRC_EXACT
        if score >= 0.90:
            return MatchConfidence.HIGH
        duration_ok = (
            abs(duration_ms - cand_ms) <= MusicPatterns.DURATION_TOLERANCE_MS
            if duration_ms and cand_ms else True
        )
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
        if c >= 0.90: return MatchConfidence.HIGH
        if c >= 0.75: return MatchConfidence.GOOD
        if c >= 0.55: return MatchConfidence.LOW
        return MatchConfidence.NONE

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        for provider in (self.mb, self.itunes, self.deezer):
            if hasattr(provider, "close"):
                try:
                    await provider.close()
                except Exception:
                    pass

    # Pipeline/MetadataPipeline.py — import in cima al file
    @staticmethod
    def _key_word(norm: str) -> str:
        words = [w for w in norm.split() if w not in TextCleaner._STOPWORDS]
        return max(words, key=len) if words else (norm.split()[0] if norm else "")

    async def _db_candidate_rows(self, title_norm: str, artist_norm: str) -> list[AppleMusicTrack]:
        """Pre-filtro SQL (word LIKE) invece di limit(200) arbitrario, che scartava
        ogni riga oltre le prime 200 senza alcun criterio."""
        conds = []
        if artist_norm:
            conds.append(AppleMusicTrack.artist_name.ilike(f"%{self._key_word(artist_norm)}%"))
        if title_norm:
            conds.append(AppleMusicTrack.track_name.ilike(f"%{self._key_word(title_norm)}%"))
        if not conds:
            return []
        stmt = select(AppleMusicTrack).where(or_(*conds)).limit(3000)
        result = await self.db_session.execute(stmt)
        return list(result.scalars().all())


    async def _db_track_by_title_artist(
        self, title_norm: str, artist_norm: str, album_hint: str = "",
        duration_ms: Optional[int] = None,
    ) -> Optional[AppleMusicTrack]:
        try:
            rows = await self._db_candidate_rows(title_norm, artist_norm)
        except Exception as exc:
            self.log.debug(f"[Pipeline][DB] select tracks fallito: {exc}")
            return None
        if not rows:
            return None

        album_norm = TextCleaner.normalize(album_hint) if album_hint else ""
        best_track, best_score = None, -1.0

        for row in rows:
            candidate = {
                "trackName": row.track_name or "",
                "artistName": row.artist_name or "",
                "collectionName": row.collection_name or "",
                "trackTimeMillis": row.track_time_millis,
            }
            score = self.matcher.score_candidate(
                title=title_norm, artist=artist_norm, album_hint=album_norm,
                duration_ms=duration_ms, isrc="", candidate=candidate,
            )
            if score is not None and score > best_score:
                best_score, best_track = score, row

        if best_track:
            self.log.debug(f"[Pipeline][DB] Hit: '{best_track.track_name}' score={best_score:.2f}")
        return best_track